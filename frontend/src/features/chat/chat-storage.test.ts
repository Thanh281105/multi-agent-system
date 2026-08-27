import { describe, expect, it } from "vitest"

import {
  MAX_HISTORY_ITEMS,
  clearChatSnapshot,
  readChatSnapshot,
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
