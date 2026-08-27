import type {
  GatewayChatResponse,
  GatewayStatusEvent,
  GatewayTokenEvent,
} from "@/lib/contracts"

export interface ChatMessage {
  id: string
  role: "user" | "assistant"
  text: string
}

export interface ChatFailure {
  source: "gateway" | "client"
  code: string
  message: string
  retryable: boolean
  requestId?: string
  traceId?: string
}

export type ChatRequestState =
  | { phase: "idle"; generation: number }
  | {
      phase: "streaming"
      generation: number
      statuses: GatewayStatusEvent[]
      answer: string
      lastSequence: number
    }
  | {
      phase: "completed"
      generation: number
      result: GatewayChatResponse
    }
  | {
      phase: "failed"
      generation: number
      failure: ChatFailure
    }
  | { phase: "cancelled"; generation: number }

export interface ChatState {
  request: ChatRequestState
  sessionId: string | null
  messages: ChatMessage[]
  online: boolean
  credentialConfigured: boolean
}

export type WorkspaceState =
  | "initial"
  | "loading"
  | "success"
  | "partial"
  | "empty"
  | "error"
  | "offline"
  | "cancelled"
  | "disabled"

export type ChatAction =
  | {
      type: "history.hydrated"
      sessionId: string | null
      messages: ChatMessage[]
    }
  | { type: "connection.changed"; online: boolean }
  | { type: "credential.changed"; configured: boolean }
  | {
      type: "request.started"
      generation: number
      message: ChatMessage
    }
  | {
      type: "request.status"
      generation: number
      status: GatewayStatusEvent
    }
  | {
      type: "request.token"
      generation: number
      token: GatewayTokenEvent
    }
  | {
      type: "request.completed"
      generation: number
      result: GatewayChatResponse
      assistantMessageId: string
    }
  | {
      type: "request.failed"
      generation: number
      failure: ChatFailure
    }
  | {
      type: "request.cancelled"
      generation: number
      nextGeneration: number
    }
  | { type: "session.reset"; generation: number }

export function createInitialChatState(
  options: { online?: boolean; credentialConfigured?: boolean } = {},
): ChatState {
  return {
    request: { phase: "idle", generation: 0 },
    sessionId: null,
    messages: [],
    online: options.online ?? true,
    credentialConfigured: options.credentialConfigured ?? false,
  }
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  if (action.type === "history.hydrated") {
    if (state.request.phase !== "idle") return state
    return {
      ...state,
      sessionId: action.sessionId,
      messages: action.messages,
    }
  }

  if (action.type === "connection.changed") {
    return { ...state, online: action.online }
  }

  if (action.type === "credential.changed") {
    return { ...state, credentialConfigured: action.configured }
  }

  if (action.type === "request.started") {
    if (action.generation <= state.request.generation) return state
    return {
      ...state,
      request: {
        phase: "streaming",
        generation: action.generation,
        statuses: [],
        answer: "",
        lastSequence: 0,
      },
      messages: [...state.messages, action.message],
    }
  }

  if (action.type === "session.reset") {
    if (action.generation <= state.request.generation) return state
    return {
      ...state,
      request: { phase: "idle", generation: action.generation },
      sessionId: null,
      messages: [],
    }
  }

  if (action.generation !== state.request.generation) return state

  if (action.type === "request.status") {
    if (
      state.request.phase !== "streaming" ||
      action.status.sequence <= state.request.lastSequence
    ) {
      return state
    }
    return {
      ...state,
      request: {
        ...state.request,
        statuses: [...state.request.statuses, action.status],
        lastSequence: action.status.sequence,
      },
    }
  }

  if (action.type === "request.token") {
    if (
      state.request.phase !== "streaming" ||
      action.token.sequence <= state.request.lastSequence
    ) {
      return state
    }
    return {
      ...state,
      request: {
        ...state.request,
        answer: state.request.answer + action.token.delta,
        lastSequence: action.token.sequence,
      },
    }
  }

  if (action.type === "request.completed") {
    if (state.request.phase !== "streaming") return state
    const answer = action.result.answer.trim()
    return {
      ...state,
      request: {
        phase: "completed",
        generation: action.generation,
        result: action.result,
      },
      sessionId: action.result.session_id,
      messages: answer
        ? [
            ...state.messages,
            { id: action.assistantMessageId, role: "assistant", text: answer },
          ]
        : state.messages,
    }
  }

  if (action.type === "request.failed") {
    if (state.request.phase !== "streaming") return state
    return {
      ...state,
      request: {
        phase: "failed",
        generation: action.generation,
        failure: action.failure,
      },
      sessionId:
        action.failure.code === "gateway.session_not_found"
          ? null
          : state.sessionId,
    }
  }

  if (action.type === "request.cancelled") {
    if (
      state.request.phase !== "streaming" ||
      action.nextGeneration <= action.generation
    ) {
      return state
    }
    return {
      ...state,
      request: { phase: "cancelled", generation: action.nextGeneration },
    }
  }

  return state
}

export function selectWorkspaceState(state: ChatState): WorkspaceState {
  if (!state.online) return "offline"
  if (!state.credentialConfigured) return "disabled"

  if (state.request.phase === "idle") return "initial"
  if (state.request.phase === "streaming") return "loading"
  if (state.request.phase === "failed") return "error"
  if (state.request.phase === "cancelled") return "cancelled"

  if (state.request.result.status === "partial_success") return "partial"
  if (
    state.request.result.status === "failed" ||
    state.request.result.status === "pending" ||
    state.request.result.status === "running"
  ) {
    return "error"
  }
  return state.request.result.answer.trim() ? "success" : "empty"
}

export function canSubmitMessage(state: ChatState): boolean {
  return (
    state.online &&
    state.credentialConfigured &&
    state.request.phase !== "streaming"
  )
}
