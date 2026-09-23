import { z } from "zod/mini"

import type { ChatMessage } from "@/features/chat/chat-state"
import type { ConversationMode } from "@/lib/v2-contracts"

const SESSION_ID_KEY = "thuong-tri.session-id"
const HISTORY_KEY = "thuong-tri.history"
export const DURABLE_CHAT_METADATA_KEY = "thuong-tri.chat-metadata.v2"
export const LEGACY_DURABLE_CHAT_METADATA_KEY = "thuong-tri.chat-metadata"
export const DURABLE_CHAT_STORAGE_VERSION = 2 as const
export const MAX_HISTORY_ITEMS = 24

const sessionIdSchema = z
  .string()
  .check(z.regex(/^sess_[a-zA-Z0-9_-]{3,120}$/))
const persistedMessageSchema = z.strictObject({
  id: z.optional(z.string().check(z.minLength(1))),
  role: z.enum(["user", "assistant"]),
  text: z.string().check(z.minLength(1), z.maxLength(20_000)),
})
const durableIdentifierSchema = z
  .string()
  .check(z.regex(/^[a-z][a-z0-9_-]{2,127}$/))
const clientTurnIdSchema = z
  .string()
  .check(z.regex(/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/))
const storageScopeIdSchema = z
  .string()
  .check(z.minLength(1), z.maxLength(200), z.regex(/^[^\s]+$/))
const conversationModeSchema = z.enum(["shopper", "merchant"])
const pendingRecoverySchema = z.strictObject({
  conversationId: durableIdentifierSchema,
  clientTurnId: clientTurnIdSchema,
  message: z
    .string()
    .check(
      z.minLength(1),
      z.maxLength(2_000),
      z.refine((value) => value.trim().length > 0),
    ),
})
const durableMetadataSchema = z.strictObject({
  version: z.literal(DURABLE_CHAT_STORAGE_VERSION),
  scopeId: storageScopeIdSchema,
  selectedMode: conversationModeSchema,
  selectedConversationId: z.nullable(durableIdentifierSchema),
  pendingRecovery: z.nullable(pendingRecoverySchema),
})
const legacyDurableMetadataSchema = z.strictObject({
  version: z.literal(1),
  scopeId: storageScopeIdSchema,
  mode: conversationModeSchema,
  conversationId: z.nullable(durableIdentifierSchema),
  pendingTurn: z.nullable(pendingRecoverySchema),
})

export type DurableConversationMode = ConversationMode

export interface DurableChatStorageScope {
  /** Opaque account/credential scope identifier; never pass a credential itself. */
  scopeId: string
  mode: DurableConversationMode
}

export interface PendingTurnRecovery {
  conversationId: string
  clientTurnId: string
  message: string
}

export interface DurableChatMetadata {
  version: typeof DURABLE_CHAT_STORAGE_VERSION
  selectedMode: DurableConversationMode
  selectedConversationId: string | null
  pendingRecovery: PendingTurnRecovery | null
}

export interface ChatSnapshot {
  sessionId: string | null
  messages: ChatMessage[]
}

export interface SessionStorageAdapter {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export function readChatSnapshot(storage: SessionStorageAdapter): ChatSnapshot {
  let sessionId: string | null = null
  let messages: ChatMessage[] = []

  try {
    const storedSessionId = storage.getItem(SESSION_ID_KEY)
    const parsedSessionId = sessionIdSchema.safeParse(storedSessionId)
    if (parsedSessionId.success) sessionId = parsedSessionId.data

    const rawHistory = storage.getItem(HISTORY_KEY)
    if (rawHistory) {
      const parsedJson: unknown = JSON.parse(rawHistory)
      if (Array.isArray(parsedJson)) {
        messages = parsedJson
          .map((item, index) => {
            const parsed = persistedMessageSchema.safeParse(item)
            if (!parsed.success) return null
            return {
              id: parsed.data.id ?? `restored-${index}`,
              role: parsed.data.role,
              text: parsed.data.text,
            } satisfies ChatMessage
          })
          .filter((item): item is ChatMessage => item !== null)
          .slice(-MAX_HISTORY_ITEMS)
      }
    }
  } catch {
    return { sessionId: null, messages: [] }
  }

  return { sessionId, messages }
}

export function writeChatSnapshot(
  storage: SessionStorageAdapter,
  snapshot: ChatSnapshot,
): boolean {
  try {
    if (snapshot.sessionId) {
      storage.setItem(SESSION_ID_KEY, snapshot.sessionId)
    } else {
      storage.removeItem(SESSION_ID_KEY)
    }
    storage.setItem(
      HISTORY_KEY,
      JSON.stringify(snapshot.messages.slice(-MAX_HISTORY_ITEMS)),
    )
    return true
  } catch {
    return false
  }
}

export function clearChatSnapshot(storage: SessionStorageAdapter): boolean {
  try {
    storage.removeItem(SESSION_ID_KEY)
    storage.removeItem(HISTORY_KEY)
    return true
  } catch {
    return false
  }
}

export function readDurableChatMetadata(
  storage: SessionStorageAdapter,
  scope: DurableChatStorageScope,
): DurableChatMetadata {
  const empty = emptyDurableMetadata(scope.mode)
  if (!validScope(scope)) return empty

  try {
    const raw = storage.getItem(DURABLE_CHAT_METADATA_KEY)
    if (raw) {
      const parsed = durableMetadataSchema.safeParse(JSON.parse(raw) as unknown)
      if (!parsed.success || !validPendingSelection(parsed.data)) {
        storage.removeItem(DURABLE_CHAT_METADATA_KEY)
        return empty
      }
      if (
        parsed.data.scopeId !== scope.scopeId ||
        parsed.data.selectedMode !== scope.mode
      ) {
        storage.removeItem(DURABLE_CHAT_METADATA_KEY)
        return empty
      }
      return {
        version: DURABLE_CHAT_STORAGE_VERSION,
        selectedMode: parsed.data.selectedMode,
        selectedConversationId: parsed.data.selectedConversationId,
        pendingRecovery: parsed.data.pendingRecovery,
      }
    }

    return migrateLegacyDurableMetadata(storage, scope)
  } catch {
    discardInvalidDurableMetadata(storage)
    return empty
  }
}

export function writeDurableChatMetadata(
  storage: SessionStorageAdapter,
  scope: DurableChatStorageScope,
  metadata: Omit<DurableChatMetadata, "version" | "selectedMode">,
): boolean {
  if (!validScope(scope)) return false
  const safePayload = {
    version: DURABLE_CHAT_STORAGE_VERSION,
    scopeId: scope.scopeId,
    selectedMode: scope.mode,
    selectedConversationId: metadata.selectedConversationId,
    pendingRecovery: metadata.pendingRecovery
      ? {
          conversationId: metadata.pendingRecovery.conversationId,
          clientTurnId: metadata.pendingRecovery.clientTurnId,
          message: metadata.pendingRecovery.message,
        }
      : null,
  }
  const parsed = durableMetadataSchema.safeParse(safePayload)
  if (!parsed.success || !validPendingSelection(parsed.data)) return false

  try {
    storage.setItem(DURABLE_CHAT_METADATA_KEY, JSON.stringify(parsed.data))
    storage.removeItem(LEGACY_DURABLE_CHAT_METADATA_KEY)
    return true
  } catch {
    return false
  }
}

export function clearDurableChatMetadata(
  storage: SessionStorageAdapter,
): boolean {
  try {
    storage.removeItem(DURABLE_CHAT_METADATA_KEY)
    storage.removeItem(LEGACY_DURABLE_CHAT_METADATA_KEY)
    return true
  } catch {
    return false
  }
}

function migrateLegacyDurableMetadata(
  storage: SessionStorageAdapter,
  scope: DurableChatStorageScope,
): DurableChatMetadata {
  const empty = emptyDurableMetadata(scope.mode)
  const raw = storage.getItem(LEGACY_DURABLE_CHAT_METADATA_KEY)
  if (!raw) return empty
  const parsed = legacyDurableMetadataSchema.safeParse(JSON.parse(raw) as unknown)
  if (
    !parsed.success ||
    parsed.data.scopeId !== scope.scopeId ||
    parsed.data.mode !== scope.mode ||
    !validPendingSelection({
      selectedConversationId: parsed.data.conversationId,
      pendingRecovery: parsed.data.pendingTurn,
    })
  ) {
    storage.removeItem(LEGACY_DURABLE_CHAT_METADATA_KEY)
    return empty
  }

  const migrated = {
    selectedConversationId: parsed.data.conversationId,
    pendingRecovery: parsed.data.pendingTurn,
  }
  if (!writeDurableChatMetadata(storage, scope, migrated)) return empty
  return {
    version: DURABLE_CHAT_STORAGE_VERSION,
    selectedMode: scope.mode,
    ...migrated,
  }
}

function emptyDurableMetadata(
  mode: DurableConversationMode,
): DurableChatMetadata {
  return {
    version: DURABLE_CHAT_STORAGE_VERSION,
    selectedMode: mode,
    selectedConversationId: null,
    pendingRecovery: null,
  }
}

function validScope(scope: DurableChatStorageScope): boolean {
  return (
    storageScopeIdSchema.safeParse(scope.scopeId).success &&
    conversationModeSchema.safeParse(scope.mode).success
  )
}

function validPendingSelection(value: {
  selectedConversationId: string | null
  pendingRecovery: PendingTurnRecovery | null
}): boolean {
  return (
    value.pendingRecovery === null ||
    value.pendingRecovery.conversationId === value.selectedConversationId
  )
}

function discardInvalidDurableMetadata(storage: SessionStorageAdapter): void {
  try {
    storage.removeItem(DURABLE_CHAT_METADATA_KEY)
    storage.removeItem(LEGACY_DURABLE_CHAT_METADATA_KEY)
  } catch {
    // A blocked storage adapter is already isolated from the application.
  }
}
