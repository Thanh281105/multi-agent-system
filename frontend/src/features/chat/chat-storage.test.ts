import { describe, expect, it } from "vitest"

import {
  DURABLE_CHAT_METADATA_KEY,
  DURABLE_CHAT_STORAGE_VERSION,
  LEGACY_DURABLE_CHAT_METADATA_KEY,
  MAX_HISTORY_ITEMS,
  clearDurableChatMetadata,
  clearChatSnapshot,
  readDurableChatMetadata,
  readChatSnapshot,
  writeDurableChatMetadata,
  writeChatSnapshot,
  type SessionStorageAdapter,
} from "@/features/chat/chat-storage"

describe("chat session storage", () => {
  it("reads the legacy history shape and discards invalid data", () => {
    const storage = createStorage({
      "thuong-tri.session-id": "sess_existing_123",
      "thuong-tri.history": JSON.stringify([
        { role: "user", text: "Câu hỏi cũ" },
        { role: "system", text: "must be ignored" },
        { role: "assistant", text: "Câu trả lời cũ", extra: "reject" },
      ]),
    })

    expect(readChatSnapshot(storage)).toEqual({
      sessionId: "sess_existing_123",
      messages: [
        { id: "restored-0", role: "user", text: "Câu hỏi cũ" },
      ],
    })
  })

  it("bounds history and never writes an API-key storage entry", () => {
    const storage = createStorage()
    const messages = Array.from({ length: MAX_HISTORY_ITEMS + 4 }, (_, index) => ({
      id: `message-${index}`,
      role: index % 2 === 0 ? ("user" as const) : ("assistant" as const),
      text: `message ${index}`,
    }))

    expect(
      writeChatSnapshot(storage, {
        sessionId: "sess_existing_123",
        messages,
      }),
    ).toBe(true)

    const dump = storage.dump()
    expect(Object.keys(dump)).toEqual([
      "thuong-tri.session-id",
      "thuong-tri.history",
    ])
    expect(JSON.parse(dump["thuong-tri.history"])).toHaveLength(
      MAX_HISTORY_ITEMS,
    )
    expect(JSON.stringify(dump).toLowerCase()).not.toContain("api-key")
  })

  it("clears both safe keys and tolerates blocked storage", () => {
    const storage = createStorage({
      "thuong-tri.session-id": "sess_existing_123",
      "thuong-tri.history": "[]",
    })
    expect(clearChatSnapshot(storage)).toBe(true)
    expect(storage.dump()).toEqual({})

    const blocked: SessionStorageAdapter = {
      getItem() {
        throw new Error("blocked")
      },
      setItem() {
        throw new Error("blocked")
      },
      removeItem() {
        throw new Error("blocked")
      },
    }
    expect(readChatSnapshot(blocked)).toEqual({
      sessionId: null,
      messages: [],
    })
    expect(writeChatSnapshot(blocked, { sessionId: null, messages: [] })).toBe(
      false,
    )
    expect(clearChatSnapshot(blocked)).toBe(false)
  })
})

describe("durable chat metadata storage", () => {
  const shopperScope = { scopeId: "account-a.credential-1", mode: "shopper" as const }

  it("round-trips only versioned selection and exact pending recovery metadata", () => {
    const storage = createStorage()
    const pendingRecovery = {
      conversationId: "conversation_123",
      clientTurnId: "client-turn:stable.123",
      message: "Giữ nguyên câu hỏi này để kết nối lại",
    }

    expect(
      writeDurableChatMetadata(storage, shopperScope, {
        selectedConversationId: "conversation_123",
        pendingRecovery,
      }),
    ).toBe(true)
    expect(readDurableChatMetadata(storage, shopperScope)).toEqual({
      version: DURABLE_CHAT_STORAGE_VERSION,
      selectedMode: "shopper",
      selectedConversationId: "conversation_123",
      pendingRecovery,
    })

    const persisted = JSON.parse(storage.dump()[DURABLE_CHAT_METADATA_KEY])
    expect(persisted).toEqual({
      version: 2,
      scopeId: shopperScope.scopeId,
      selectedMode: "shopper",
      selectedConversationId: "conversation_123",
      pendingRecovery,
    })
  })

  it("allowlists persisted fields and never stores credentials or rich transcript data", () => {
    const storage = createStorage()
    const unsafeMetadata = {
      selectedConversationId: "conversation_123",
      pendingRecovery: null,
      apiKey: "sk-secret-value",
      authority: { allowedModes: ["merchant"] },
      messages: [{ role: "assistant", text: "full transcript" }],
      citations: [{ citationId: "citation_1" }],
      artifacts: [{ rawToolPayload: "secret raw payload" }],
    }

    expect(
      writeDurableChatMetadata(
        storage,
        shopperScope,
        unsafeMetadata,
      ),
    ).toBe(true)
    const serialized = storage.dump()[DURABLE_CHAT_METADATA_KEY]
    expect(serialized).not.toContain("sk-secret-value")
    expect(serialized).not.toContain("full transcript")
    expect(serialized).not.toContain("citation")
    expect(serialized).not.toContain("artifact")
    expect(serialized).not.toContain("authority")
  })

  it("isolates account, credential, and mode changes by clearing mismatched data", () => {
    const storage = createStorage()
    expect(
      writeDurableChatMetadata(storage, shopperScope, {
        selectedConversationId: "conversation_123",
        pendingRecovery: null,
      }),
    ).toBe(true)

    expect(
      readDurableChatMetadata(storage, {
        scopeId: "account-a.credential-2",
        mode: "shopper",
      }),
    ).toEqual({
      version: 2,
      selectedMode: "shopper",
      selectedConversationId: null,
      pendingRecovery: null,
    })
    expect(storage.dump()[DURABLE_CHAT_METADATA_KEY]).toBeUndefined()

    expect(
      writeDurableChatMetadata(storage, shopperScope, {
        selectedConversationId: "conversation_456",
        pendingRecovery: null,
      }),
    ).toBe(true)
    expect(
      readDurableChatMetadata(storage, {
        scopeId: shopperScope.scopeId,
        mode: "merchant",
      }).selectedConversationId,
    ).toBeNull()
    expect(storage.dump()[DURABLE_CHAT_METADATA_KEY]).toBeUndefined()
  })

  it("migrates safe v1 metadata and discards invalid or inconsistent entries", () => {
    const storage = createStorage({
      [LEGACY_DURABLE_CHAT_METADATA_KEY]: JSON.stringify({
        version: 1,
        scopeId: shopperScope.scopeId,
        mode: "shopper",
        conversationId: "conversation_123",
        pendingTurn: {
          conversationId: "conversation_123",
          clientTurnId: "client-turn:stable.123",
          message: "Khôi phục lượt đang chạy",
        },
      }),
    })

    expect(readDurableChatMetadata(storage, shopperScope)).toMatchObject({
      version: 2,
      selectedConversationId: "conversation_123",
      pendingRecovery: { clientTurnId: "client-turn:stable.123" },
    })
    expect(storage.dump()[LEGACY_DURABLE_CHAT_METADATA_KEY]).toBeUndefined()
    expect(storage.dump()[DURABLE_CHAT_METADATA_KEY]).toBeDefined()

    const invalid = createStorage({
      [DURABLE_CHAT_METADATA_KEY]: JSON.stringify({
        version: 2,
        scopeId: shopperScope.scopeId,
        selectedMode: "shopper",
        selectedConversationId: "conversation_123",
        pendingRecovery: {
          conversationId: "conversation_other",
          clientTurnId: "client-turn:stable.123",
          message: "wrong conversation",
        },
      }),
    })
    expect(readDurableChatMetadata(invalid, shopperScope).pendingRecovery).toBeNull()
    expect(invalid.dump()[DURABLE_CHAT_METADATA_KEY]).toBeUndefined()

    const malformed = createStorage({
      [LEGACY_DURABLE_CHAT_METADATA_KEY]: "{not-json",
    })
    expect(readDurableChatMetadata(malformed, shopperScope).pendingRecovery).toBeNull()
    expect(malformed.dump()[LEGACY_DURABLE_CHAT_METADATA_KEY]).toBeUndefined()
  })

  it("clears durable keys without deleting v1 compatibility data", () => {
    const storage = createStorage({
      [DURABLE_CHAT_METADATA_KEY]: "{}",
      [LEGACY_DURABLE_CHAT_METADATA_KEY]: "{}",
      "thuong-tri.session-id": "sess_existing_123",
      "thuong-tri.history": "[]",
    })
    expect(clearDurableChatMetadata(storage)).toBe(true)
    expect(storage.dump()).toEqual({
      "thuong-tri.session-id": "sess_existing_123",
      "thuong-tri.history": "[]",
    })
  })

  it("fails closed when durable storage is blocked", () => {
    const blocked: SessionStorageAdapter = {
      getItem() {
        throw new Error("blocked")
      },
      setItem() {
        throw new Error("blocked")
      },
      removeItem() {
        throw new Error("blocked")
      },
    }
    expect(readDurableChatMetadata(blocked, shopperScope)).toEqual({
      version: 2,
      selectedMode: "shopper",
      selectedConversationId: null,
      pendingRecovery: null,
    })
    expect(
      writeDurableChatMetadata(blocked, shopperScope, {
        selectedConversationId: null,
        pendingRecovery: null,
      }),
    ).toBe(false)
    expect(clearDurableChatMetadata(blocked)).toBe(false)
  })
})

function createStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem(key: string) {
      return values.get(key) ?? null
    },
    setItem(key: string, value: string) {
      values.set(key, value)
    },
    removeItem(key: string) {
      values.delete(key)
    },
    dump() {
      return Object.fromEntries(values)
    },
  }
}
