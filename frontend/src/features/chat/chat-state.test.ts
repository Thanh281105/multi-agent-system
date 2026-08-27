import { describe, expect, it } from "vitest"

import {
  canSubmitMessage,
  chatReducer,
  createInitialChatState,
  selectWorkspaceState,
  type ChatFailure,
} from "@/features/chat/chat-state"
import { completedResponse, statusEvent, tokenEvent } from "@/test/fixtures"

const userMessage = {
  id: "message-user-1",
  role: "user" as const,
  text: "Tìm tai nghe",
}

describe("chatReducer", () => {
  it("accepts ordered progress and ignores stale or duplicate events", () => {
    const started = chatReducer(createReadyState(), {
      type: "request.started",
      generation: 1,
      message: userMessage,
    })
    const progressed = chatReducer(started, {
      type: "request.status",
      generation: 1,
      status: statusEvent,
    })
    const duplicate = chatReducer(progressed, {
      type: "request.status",
      generation: 1,
      status: statusEvent,
    })
    const stale = chatReducer(progressed, {
      type: "request.status",
      generation: 0,
      status: { ...statusEvent, sequence: 2 },
    })

    expect(progressed.request).toMatchObject({
      phase: "streaming",
      lastSequence: 1,
      statuses: [statusEvent],
    })
    expect(duplicate).toBe(progressed)
    expect(stale).toBe(progressed)
  })

  it("allows one completion and ignores later terminal actions", () => {
    const started = chatReducer(createReadyState(), {
      type: "request.started",
      generation: 3,
      message: userMessage,
    })
    const completed = chatReducer(started, {
      type: "request.completed",
      generation: 3,
      result: completedResponse,
      assistantMessageId: "message-assistant-1",
    })
    const lateFailure = chatReducer(completed, {
      type: "request.failed",
      generation: 3,
      failure: failure,
    })

    expect(completed.request).toMatchObject({ phase: "completed" })
    expect(completed.sessionId).toBe("sess_test_123")
    expect(completed.messages.at(-1)).toEqual({
      id: "message-assistant-1",
      role: "assistant",
      text: completedResponse.answer,
    })
    expect(lateFailure).toBe(completed)
  })

  it("appends ordered answer deltas and ignores stale generations", () => {
    const started = chatReducer(createReadyState(), {
      type: "request.started",
      generation: 2,
      message: userMessage,
    })
    const firstDelta = chatReducer(started, {
      type: "request.token",
      generation: 2,
      token: tokenEvent,
    })
    const staleDelta = chatReducer(firstDelta, {
      type: "request.token",
      generation: 1,
      token: { ...tokenEvent, sequence: 3, delta: "stale" },
    })
    const secondDelta = chatReducer(firstDelta, {
      type: "request.token",
      generation: 2,
      token: { ...tokenEvent, sequence: 3, delta: "phù hợp." },
    })

    expect(firstDelta.request).toMatchObject({
      phase: "streaming",
      answer: "Tai nghe ",
      lastSequence: 2,
    })
    expect(staleDelta).toBe(firstDelta)
    expect(secondDelta.request).toMatchObject({
      answer: "Tai nghe phù hợp.",
      lastSequence: 3,
    })
  })

  it("increments the generation on cancellation so late work is stale", () => {
    const started = chatReducer(createReadyState(), {
      type: "request.started",
      generation: 5,
      message: userMessage,
    })
    const cancelled = chatReducer(started, {
      type: "request.cancelled",
      generation: 5,
      nextGeneration: 6,
    })
    const lateCompletion = chatReducer(cancelled, {
      type: "request.completed",
      generation: 5,
      result: completedResponse,
      assistantMessageId: "late",
    })

    expect(cancelled.request).toEqual({
      phase: "cancelled",
      generation: 6,
    })
    expect(lateCompletion).toBe(cancelled)
  })

  it("clears an expired session but retains it for other failures", () => {
    const withSession = {
      ...createReadyState(),
      sessionId: "sess_existing_123",
    }
    const started = chatReducer(withSession, {
      type: "request.started",
      generation: 1,
      message: userMessage,
    })
    const expired = chatReducer(started, {
      type: "request.failed",
      generation: 1,
      failure: { ...failure, code: "gateway.session_not_found" },
    })

    expect(expired.sessionId).toBeNull()

    const secondStart = chatReducer(withSession, {
      type: "request.started",
      generation: 2,
      message: userMessage,
    })
    const networkFailure = chatReducer(secondStart, {
      type: "request.failed",
      generation: 2,
      failure,
    })
    expect(networkFailure.sessionId).toBe("sess_existing_123")
  })

  it("hydrates only while idle and resets with a newer generation", () => {
    const hydrated = chatReducer(createReadyState(), {
      type: "history.hydrated",
      sessionId: "sess_existing_123",
      messages: [userMessage],
    })
    const started = chatReducer(hydrated, {
      type: "request.started",
      generation: 1,
      message: { ...userMessage, id: "message-user-2" },
    })
    const ignoredHydration = chatReducer(started, {
      type: "history.hydrated",
      sessionId: null,
      messages: [],
    })
    const reset = chatReducer(started, {
      type: "session.reset",
      generation: 2,
    })

    expect(hydrated.sessionId).toBe("sess_existing_123")
    expect(ignoredHydration).toBe(started)
    expect(reset).toMatchObject({ sessionId: null, messages: [] })
    expect(reset.request).toEqual({ phase: "idle", generation: 2 })
  })
})

describe("chat state selectors", () => {
  it("covers disabled, offline, initial, and loading states", () => {
    const disabled = createInitialChatState()
    const ready = createReadyState()
    const loading = chatReducer(ready, {
      type: "request.started",
      generation: 1,
      message: userMessage,
    })

    expect(selectWorkspaceState(disabled)).toBe("disabled")
    expect(
      selectWorkspaceState(
        chatReducer(ready, { type: "connection.changed", online: false }),
      ),
    ).toBe("offline")
    expect(selectWorkspaceState(ready)).toBe("initial")
    expect(
      selectWorkspaceState(
        chatReducer(disabled, {
          type: "credential.changed",
          configured: true,
        }),
      ),
    ).toBe("initial")
    expect(selectWorkspaceState(loading)).toBe("loading")
    expect(canSubmitMessage(ready)).toBe(true)
    expect(canSubmitMessage(loading)).toBe(false)
  })

  it("derives success, partial, empty, error, and cancelled states", () => {
    expect(completeWith(completedResponse)).toBe("success")
    expect(
      completeWith({ ...completedResponse, status: "partial_success" }),
    ).toBe("partial")
    expect(completeWith({ ...completedResponse, answer: "" })).toBe("empty")
    expect(completeWith({ ...completedResponse, status: "failed" })).toBe(
      "error",
    )

    const started = chatReducer(createReadyState(), {
      type: "request.started",
      generation: 7,
      message: userMessage,
    })
    const failed = chatReducer(started, {
      type: "request.failed",
      generation: 7,
      failure,
    })
    const cancelled = chatReducer(started, {
      type: "request.cancelled",
      generation: 7,
      nextGeneration: 8,
    })
    expect(selectWorkspaceState(failed)).toBe("error")
    expect(selectWorkspaceState(cancelled)).toBe("cancelled")
  })
})

const failure: ChatFailure = {
  source: "client",
  code: "gateway.network_error",
  message: "Không thể kết nối đến Gateway.",
  retryable: true,
}

function createReadyState() {
  return createInitialChatState({ credentialConfigured: true })
}

function completeWith(result: typeof completedResponse) {
  const started = chatReducer(createReadyState(), {
    type: "request.started",
    generation: 1,
    message: userMessage,
  })
  return selectWorkspaceState(
    chatReducer(started, {
      type: "request.completed",
      generation: 1,
      result,
      assistantMessageId: "assistant",
    }),
  )
}
