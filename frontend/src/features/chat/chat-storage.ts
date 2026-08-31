import { z } from "zod/mini"

import type { ChatMessage } from "@/features/chat/chat-state"

const SESSION_ID_KEY = "thuong-tri.session-id"
const HISTORY_KEY = "thuong-tri.history"
export const MAX_HISTORY_ITEMS = 24

const sessionIdSchema = z
  .string()
  .check(z.regex(/^sess_[a-zA-Z0-9_-]{3,120}$/))
const persistedMessageSchema = z.strictObject({
  id: z.optional(z.string().check(z.minLength(1))),
  role: z.enum(["user", "assistant"]),
  text: z.string().check(z.minLength(1), z.maxLength(20_000)),
})

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
