import { describe, expect, it } from "vitest"

import {
  canSubmitMessage,
  chatReducer,
  createDurableRecoveryRequest,
  createInitialChatState,
  selectDurableWorkspaceState,
  selectWorkspaceState,
  type ChatFailure,
} from "@/features/chat/chat-state"
import { completedResponse, statusEvent, tokenEvent } from "@/test/fixtures"
import type {
  ActionCard,
  ConversationSummary,
  HistoryTurn,
  PreferenceRecord,
  TurnArtifact,
  TurnResponse,
  TurnResult,
  TurnSseEvent,
} from "@/lib/v2-contracts"

const userMessage = {
  id: "message-user-1",
  role: "user" as const,
  text: "Tìm sách chiêm tinh",
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
      answer: "Nhật Ký Tarot ",
      lastSequence: 2,
    })
    expect(staleDelta).toBe(firstDelta)
    expect(secondDelta.request).toMatchObject({
      answer: "Nhật Ký Tarot phù hợp.",
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

describe("durable v2 chat state", () => {
  it("retains the exact recovery identity and message across transport retry", () => {
    const started = startDurableTurn(1)
    const failed = chatReducer(started, {
      type: "durable.turn.transport-failed",
      generation: 1,
      failure,
    })
    const retried = chatReducer(failed, {
      type: "durable.turn.retried",
      generation: 2,
    })

    expect(createDurableRecoveryRequest(retried)).toEqual({
      conversationId: "conversation_123",
      clientTurnId: "client-turn:stable.123",
      message: "Tìm sách bền vững",
    })
    expect(retried.durable.activeTurn).toMatchObject({
      generation: 2,
      turnId: null,
      status: "pending",
    })
    expect(retried.messages).toEqual([])
    expect(retried.durable.failure).toBeNull()
  })

  it("ignores stale generations, wrong turn identities, and replayed sequences", () => {
    const started = startDurableTurn(1)
    const progressed = chatReducer(started, {
      type: "durable.turn.event",
      generation: 1,
      event: durableProgressEvent,
    })
    const staleGeneration = chatReducer(progressed, {
      type: "durable.turn.event",
      generation: 0,
      event: { ...durableProgressEvent, sequence: 2 },
    })
    const wrongTurn = chatReducer(progressed, {
      type: "durable.turn.event",
      generation: 1,
      event: {
        ...durableProgressEvent,
        sequence: 2,
        turnId: "turn_other",
      },
    })
    const replay = chatReducer(progressed, {
      type: "durable.turn.event",
      generation: 1,
      event: durableProgressEvent,
    })

    expect(progressed.durable.activeTurn).toMatchObject({
      turnId: "turn_123",
      status: "running",
      lastSequence: 1,
    })
    expect(staleGeneration).toBe(progressed)
    expect(wrongTurn).toBe(progressed)
    expect(replay).toBe(progressed)
  })

  it("accepts one terminal event without appending duplicate assistant messages", () => {
    const progressed = chatReducer(startDurableTurn(1), {
      type: "durable.turn.event",
      generation: 1,
      event: durableProgressEvent,
    })
    const completed = chatReducer(progressed, {
      type: "durable.turn.event",
      generation: 1,
      event: completedTerminalEvent,
    })
    const duplicate = chatReducer(completed, {
      type: "durable.turn.event",
      generation: 1,
      event: { ...completedTerminalEvent, sequence: 3 },
    })

    expect(completed.durable.terminalResult).toBe(durableResult)
    expect(completed.durable.activeTurn).toMatchObject({
      status: "completed",
      lastSequence: 2,
    })
    expect(completed.durable.resources.byTurnId.turn_123).toEqual({
      artifactIds: ["artifact_123"],
      actionIds: ["action_123"],
      preferenceIds: [],
    })
    expect(completed.messages).toEqual([])
    expect(duplicate).toBe(completed)
  })

  it("keeps an unsettled terminal recoverable and accepts the same sequence after reattach", () => {
    const progressed = chatReducer(startDurableTurn(1), {
      type: "durable.turn.event",
      generation: 1,
      event: durableProgressEvent,
    })
    const interrupted = chatReducer(progressed, {
      type: "durable.turn.event",
      generation: 1,
      event: unsettledInterruptedTerminalEvent,
    })

    expect(interrupted.durable.activeTurn).toMatchObject({
      status: "interrupted",
      serverSettled: false,
      lastSequence: 2,
    })
    expect(interrupted.durable.terminalResult).toBeNull()
    expect(selectDurableWorkspaceState(interrupted)).toBe("error")
    expect(canSubmitMessage(interrupted)).toBe(false)
    expect(createDurableRecoveryRequest(interrupted)).toEqual({
      conversationId: "conversation_123",
      clientTurnId: "client-turn:stable.123",
      message: "Tìm sách bền vững",
    })

    const retried = chatReducer(interrupted, {
      type: "durable.turn.retried",
      generation: 2,
    })
    expect(retried.durable.activeTurn).toMatchObject({
      generation: 2,
      lastSequence: 0,
      serverSettled: false,
    })
    const settled = chatReducer(retried, {
      type: "durable.turn.event",
      generation: 2,
      event: completedTerminalEvent,
    })
    expect(settled.durable.activeTurn?.serverSettled).toBe(true)
    expect(settled.durable.terminalResult).toBe(durableResult)

    const cancelled = chatReducer(progressed, {
      type: "durable.turn.event",
      generation: 1,
      event: { ...cancelledTerminalEvent, serverSettled: false },
    })
    expect(selectDurableWorkspaceState(cancelled)).toBe("cancelled")
    expect(canSubmitMessage(cancelled)).toBe(false)
  })

  it("replaces local replay state with authoritative hydrated server history", () => {
    const localCompleted = chatReducer(
      chatReducer(startDurableTurn(1), {
        type: "durable.turn.event",
        generation: 1,
        event: durableProgressEvent,
      }),
      {
        type: "durable.turn.event",
        generation: 1,
        event: completedTerminalEvent,
      },
    )
    const hydrated = chatReducer(localCompleted, {
      type: "durable.history.hydrated",
      generation: 2,
      conversation,
      turns: [serverHistoryTurn],
      preferences: [preference],
    })

    expect(hydrated.durable.history).toEqual([serverHistoryTurn])
    expect(hydrated.durable.activeTurn).toBeNull()
    expect(hydrated.durable.terminalResult).toBe(serverHistoryTurn.assistantResult)
    expect(hydrated.durable.resources.byTurnId.turn_server).toEqual({
      artifactIds: ["artifact_123"],
      actionIds: ["action_123"],
      preferenceIds: ["preference_123"],
    })
    expect(hydrated.durable.resources.byTurnId.turn_123).toBeUndefined()
    expect(hydrated.messages).toEqual([])
  })

  it("keeps local cancellation pending until the server response is reconciled", () => {
    const started = startDurableTurn(1)
    const accepted = chatReducer(started, {
      type: "durable.turn.reconciled",
      generation: 1,
      response: pendingTurnResponse,
    })
    const cancelling = chatReducer(accepted, {
      type: "durable.turn.cancel.requested",
      generation: 1,
    })
    const serverCompleted = chatReducer(cancelling, {
      type: "durable.turn.cancelled",
      generation: 1,
      response: completedTurnResponse,
    })

    expect(cancelling.durable.activeTurn?.cancellationPending).toBe(true)
    expect(selectDurableWorkspaceState(cancelling)).toBe("running")
    expect(serverCompleted.durable.activeTurn).toMatchObject({
      status: "completed",
      cancellationPending: false,
    })
    expect(serverCompleted.durable.terminalResult).toBe(durableResult)
    expect(selectDurableWorkspaceState(serverCompleted)).toBe("success")
    expect(
      chatReducer(serverCompleted, {
        type: "durable.turn.event",
        generation: 1,
        event: cancelledTerminalEvent,
      }),
    ).toBe(serverCompleted)
  })

  it("keeps turn status separate from dialogue outcome and exposes action states", () => {
    expect(serverHistoryTurn.status).toBe("completed")
    expect(serverHistoryTurn.outcome).toBe("awaiting_confirmation")

    let state = chatReducer(
      chatReducer(startDurableTurn(1), {
        type: "durable.turn.event",
        generation: 1,
        event: durableProgressEvent,
      }),
      {
        type: "durable.turn.event",
        generation: 1,
        event: completedTerminalEvent,
      },
    )

    for (const status of [
      "proposed",
      "executed",
      "rejected",
      "expired",
      "conflicted",
      "failed",
    ] as const) {
      state = chatReducer(state, {
        type: "durable.action.updated",
        action: { ...actionCard, status },
      })
      expect(state.durable.resources.actions.action_123.status).toBe(status)
    }
    expect(selectDurableWorkspaceState(state)).toBe("success")

    const conflicted = chatReducer(state, {
      type: "durable.action.updated",
      action: { ...actionCard, status: "conflicted" },
    })
    expect(selectWorkspaceState(conflicted)).toBe("conflict")
    const expired = chatReducer(conflicted, {
      type: "durable.action.updated",
      action: { ...actionCard, status: "expired" },
    })
    expect(selectWorkspaceState(expired)).toBe("expired")
  })

  it("distinguishes durable loading, running, partial, empty, error, and offline phases", () => {
    const loadingHistory = chatReducer(createReadyState(), {
      type: "durable.history.loading",
      generation: 1,
    })
    const running = chatReducer(startDurableTurn(1), {
      type: "durable.turn.event",
      generation: 1,
      event: durableProgressEvent,
    })
    const partial = chatReducer(running, {
      type: "durable.turn.event",
      generation: 1,
      event: durableTextEvent,
    })
    const empty = chatReducer(loadingHistory, {
      type: "durable.history.hydrated",
      generation: 1,
      conversation,
      turns: [],
    })
    const interrupted = chatReducer(running, {
      type: "durable.turn.reconciled",
      generation: 1,
      response: interruptedTurnResponse,
    })

    expect(selectDurableWorkspaceState(loadingHistory)).toBe("loading")
    expect(selectDurableWorkspaceState(running)).toBe("running")
    expect(selectDurableWorkspaceState(partial)).toBe("partial")
    expect(selectDurableWorkspaceState(empty)).toBe("empty")
    expect(selectDurableWorkspaceState(interrupted)).toBe("error")
    expect(
      selectDurableWorkspaceState(
        chatReducer(running, { type: "connection.changed", online: false }),
      ),
    ).toBe("offline")
    expect(canSubmitMessage(running)).toBe(false)
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

function startDurableTurn(generation: number) {
  return chatReducer(createReadyState(), {
    type: "durable.turn.started",
    generation,
    conversationId: "conversation_123",
    clientTurnId: "client-turn:stable.123",
    message: "Tìm sách bền vững",
  })
}

const conversation = {
  conversationId: "conversation_123",
  mode: "shopper",
  storeId: "store_123",
  title: "Sách bền vững",
  createdAt: "2026-09-13T01:00:00Z",
  updatedAt: "2026-09-13T01:01:00Z",
} as ConversationSummary

const actionCard = {
  actionId: "action_123",
  proposalId: "proposal_123",
  proposalVersion: 1,
  kind: "cart_change",
  status: "proposed",
  title: "Thêm sách vào giỏ",
  requiredPermission: "cart.write",
  confirmationRequired: false,
  target: {
    resourceType: "cart",
    resourceId: "cart_123",
    expectedResourceVersion: 1,
    dataVersionIds: ["catalog_123"],
  },
  changes: [
    {
      resourceType: "cart_item",
      resourceId: "cart_item_123",
      field: "quantity",
      beforeInteger: 0,
      afterInteger: 1,
      beforeText: null,
      afterText: null,
    },
  ],
  expiresAt: "2026-09-13T02:00:00Z",
} as ActionCard

const actionArtifact = {
  artifactId: "artifact_123",
  resourceId: "cart_123",
  resourceVersion: 1,
  title: "Thay đổi giỏ hàng",
  kind: "action",
  actionId: "action_123",
  proposalId: "proposal_123",
  proposalVersion: 1,
  actionKind: "cart_change",
  status: "proposed",
} as TurnArtifact

const durableResult = {
  outcome: "awaiting_confirmation",
  answer: "Tôi đã chuẩn bị thay đổi giỏ hàng.",
  claims: [],
  citations: [],
  evidence: [],
  actionCards: [actionCard],
  artifacts: [actionArtifact],
  plan: null,
  executions: [],
  warnings: [],
} as TurnResult

const durableProgressEvent = {
  event: "progress",
  sequence: 1,
  requestId: "request_123",
  traceId: "trace_123",
  turnId: "turn_123",
  phase: "claimed",
  turnStatus: "running",
  stepId: null,
  capability: null,
  planRevision: null,
  stepStatus: null,
  reused: false,
} as TurnSseEvent

const durableTextEvent = {
  event: "text_delta",
  sequence: 2,
  requestId: "request_123",
  traceId: "trace_123",
  turnId: "turn_123",
  turnStatus: "completed",
  delta: "Một phần câu trả lời",
  postGrounding: true,
} as TurnSseEvent

const completedTerminalEvent = {
  event: "terminal",
  sequence: 2,
  requestId: "request_123",
  traceId: "trace_123",
  turnId: "turn_123",
  payload: {
    status: "completed",
    result: durableResult,
    usage: emptyUsage(),
  },
  reusedResult: false,
  serverSettled: true,
} as Extract<TurnSseEvent, { event: "terminal" }>

const unsettledInterruptedTerminalEvent = {
  ...completedTerminalEvent,
  serverSettled: false,
  payload: {
    status: "interrupted",
    error: {
      code: "stream.interrupted",
      message: "Luồng kết quả bị gián đoạn. Vui lòng thử lại.",
      retryable: true,
    },
    usage: emptyUsage(),
  },
} as Extract<TurnSseEvent, { event: "terminal" }>

const cancelledTerminalEvent = {
  ...completedTerminalEvent,
  sequence: 3,
  payload: {
    status: "cancelled",
    usage: emptyUsage(),
  },
} as Extract<TurnSseEvent, { event: "terminal" }>

const serverHistoryTurn = {
  turnId: "turn_server",
  clientTurnId: "client-turn:server.123",
  status: "completed",
  outcome: "awaiting_confirmation",
  userMessage: "Câu hỏi từ server",
  assistantResult: durableResult,
  error: null,
  actionCards: [actionCard],
  createdAt: "2026-09-13T01:00:00Z",
  completedAt: "2026-09-13T01:01:00Z",
} as HistoryTurn

const preference = {
  preferenceId: "preference_123",
  sourceTurnId: "turn_server",
  preference: { kind: "genre", value: "Khoa học" },
  createdAt: "2026-09-13T01:00:00Z",
  updatedAt: "2026-09-13T01:00:00Z",
} as PreferenceRecord

const pendingTurnResponse = {
  conversationId: "conversation_123",
  turn: {
    turnId: "turn_123",
    clientTurnId: "client-turn:stable.123",
    status: "pending",
    outcome: null,
    createdAt: "2026-09-13T01:00:00Z",
    completedAt: null,
  },
  requestId: "request_123",
  traceId: "trace_123",
  result: null,
  error: null,
  usage: emptyUsage(),
} as TurnResponse

const completedTurnResponse = {
  ...pendingTurnResponse,
  turn: {
    ...pendingTurnResponse.turn,
    status: "completed",
    outcome: "awaiting_confirmation",
    completedAt: "2026-09-13T01:01:00Z",
  },
  result: durableResult,
} as TurnResponse

const interruptedTurnResponse = {
  ...pendingTurnResponse,
  turn: {
    ...pendingTurnResponse.turn,
    status: "interrupted",
    completedAt: "2026-09-13T01:01:00Z",
  },
  error: {
    code: "turn.interrupted",
    message: "Lượt bị gián đoạn.",
    retryable: true,
  },
} as TurnResponse

function emptyUsage() {
  return {
    inputTokens: 0,
    cachedInputTokens: 0,
    outputTokens: 0,
    reasoningTokens: 0,
    totalTokens: 0,
    generationCalls: 0,
    providerAttempts: 0,
    knowledgeRetrievals: 0,
    draftRepairs: 0,
    estimatedCostUsd: "0",
    knownCostUsd: "0",
    reservedCostUsd: "0",
    unknownReservedCostUsd: "0",
    unknownUsageAttempts: 0,
    fallbackUsed: false,
  }
}
