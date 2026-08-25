import { useCallback, useEffect, useReducer, useRef, useState } from "react"

import {
  chatReducer,
  createInitialChatState,
  selectWorkspaceState,
  type ChatFailure,
  type ChatState,
} from "@/features/chat/chat-state"
import {
  clearChatSnapshot,
  readChatSnapshot,
  writeChatSnapshot,
  type SessionStorageAdapter,
} from "@/features/chat/chat-storage"
import {
  GatewayClientError,
  isAbortError,
  sendGatewayMessage,
  type GatewayRequest,
} from "@/lib/gateway-stream"

type SendGatewayMessage = (request: GatewayRequest) => ReturnType<typeof sendGatewayMessage>

export interface ChatControllerDependencies {
  send?: SendGatewayMessage
  storage?: SessionStorageAdapter | null
  online?: boolean
  createId?: () => string
}

export type SendOutcome =
  | "completed"
  | "failed"
  | "cancelled"
  | "credential_required"
  | "offline"
  | "busy"
  | "invalid_message"

export interface ChatController {
  state: ChatState
  workspaceState: ReturnType<typeof selectWorkspaceState>
  storageAvailable: boolean
  configureCredential(value: string): boolean
  clearCredential(): void
  sendMessage(message: string): Promise<SendOutcome>
  cancelRequest(): void
  resetSession(): void
}

export function useChatController(
  dependencies: ChatControllerDependencies = {},
): ChatController {
  const [runtime] = useState(() => ({
    send: dependencies.send ?? sendGatewayMessage,
    storage:
      dependencies.storage === undefined
        ? resolveSessionStorage()
        : dependencies.storage,
    createId: dependencies.createId ?? createMessageId,
    initialOnline:
      dependencies.online ??
      (typeof navigator === "undefined" ? true : navigator.onLine),
  }))
  const [state, dispatch] = useReducer(
    chatReducer,
    runtime,
    (initialRuntime): ChatState => {
      const initial = createInitialChatState({
        online: initialRuntime.initialOnline,
      })
      const storage = initialRuntime.storage
      if (!storage) return initial
      const snapshot = readChatSnapshot(storage)
      return {
        ...initial,
        sessionId: snapshot.sessionId,
        messages: snapshot.messages,
      }
    },
  )
  const stateRef = useRef(state)
  const credentialRef = useRef("")
  const controllerRef = useRef<AbortController | null>(null)
  const generationRef = useRef(state.request.generation)
  const [storageAvailable, setStorageAvailable] = useState(true)

  useEffect(() => {
    stateRef.current = state
  }, [state])

  useEffect(() => {
    const storage = runtime.storage
    if (!storage) return
    const saved = writeChatSnapshot(storage, {
      sessionId: state.sessionId,
      messages: state.messages,
    })
    // oxlint-disable-next-line react/set-state-in-effect -- reflects an external storage failure in the UI.
    if (!saved) setStorageAvailable(false)
  }, [runtime.storage, state.messages, state.sessionId])

  useEffect(() => {
    if (dependencies.online !== undefined || typeof window === "undefined") {
      return
    }
    const handleOnline = () => dispatch({ type: "connection.changed", online: true })
    const handleOffline = () =>
      dispatch({ type: "connection.changed", online: false })
    window.addEventListener("online", handleOnline)
    window.addEventListener("offline", handleOffline)
    return () => {
      window.removeEventListener("online", handleOnline)
      window.removeEventListener("offline", handleOffline)
    }
  }, [dependencies.online])

  useEffect(
    () => () => {
      controllerRef.current?.abort()
    },
    [],
  )

  const configureCredential = useCallback((value: string) => {
    const cleaned = value.trim()
    if (cleaned.length < 8) return false
    credentialRef.current = cleaned
    dispatch({ type: "credential.changed", configured: true })
    return true
  }, [])

  const clearCredential = useCallback(() => {
    credentialRef.current = ""
    dispatch({ type: "credential.changed", configured: false })
  }, [])

  const sendMessage = useCallback(async (message: string): Promise<SendOutcome> => {
    const current = stateRef.current
    const cleaned = message.trim()
    if (!credentialRef.current) return "credential_required"
    if (!current.online) return "offline"
    if (controllerRef.current) return "busy"
    if (cleaned.length === 0 || cleaned.length > 2_000) return "invalid_message"

    const generation = generationRef.current + 1
    generationRef.current = generation
    const controller = new AbortController()
    controllerRef.current = controller
    dispatch({
      type: "request.started",
      generation,
      message: {
        id: runtime.createId(),
        role: "user",
        text: cleaned,
      },
    })

    try {
      const result = await runtime.send({
        message: cleaned,
        sessionId: current.sessionId,
        apiKey: credentialRef.current,
        signal: controller.signal,
        onStatus: (status) => {
          dispatch({ type: "request.status", generation, status })
        },
      })
      if (generation !== generationRef.current) return "cancelled"
      dispatch({
        type: "request.completed",
        generation,
        result,
        assistantMessageId: runtime.createId(),
      })
      return "completed"
    } catch (error) {
      if (generation !== generationRef.current || isAbortError(error)) {
        if (generation === generationRef.current) {
          const nextGeneration = generation + 1
          generationRef.current = nextGeneration
          dispatch({
            type: "request.cancelled",
            generation,
            nextGeneration,
          })
        }
        return "cancelled"
      }

      const failure = toChatFailure(error)
      if (failure.code === "gateway.authentication_failed") {
        credentialRef.current = ""
        dispatch({ type: "credential.changed", configured: false })
      }
      dispatch({ type: "request.failed", generation, failure })
      return "failed"
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }, [runtime])

  const cancelRequest = useCallback(() => {
    const controller = controllerRef.current
    if (!controller) return
    const generation = generationRef.current
    const nextGeneration = generation + 1
    generationRef.current = nextGeneration
    controller.abort()
    controllerRef.current = null
    dispatch({ type: "request.cancelled", generation, nextGeneration })
  }, [])

  const resetSession = useCallback(() => {
    controllerRef.current?.abort()
    controllerRef.current = null
    const generation = generationRef.current + 1
    generationRef.current = generation
    dispatch({ type: "session.reset", generation })
    const storage = runtime.storage
    if (storage && !clearChatSnapshot(storage)) setStorageAvailable(false)
  }, [runtime.storage])

  return {
    state,
    workspaceState: selectWorkspaceState(state),
    storageAvailable,
    configureCredential,
    clearCredential,
    sendMessage,
    cancelRequest,
    resetSession,
  }
}

function toChatFailure(error: unknown): ChatFailure {
  if (error instanceof GatewayClientError) {
    return {
      source: error.source,
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      ...(error.requestId ? { requestId: error.requestId } : {}),
      ...(error.traceId ? { traceId: error.traceId } : {}),
    }
  }
  return {
    source: "client",
    code: "gateway.unknown_client_error",
    message: "Không thể xử lý yêu cầu lúc này.",
    retryable: false,
  }
}

function resolveSessionStorage(): SessionStorageAdapter | null {
  if (typeof window === "undefined") return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

let fallbackId = 0
function createMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `message-${crypto.randomUUID()}`
  }
  fallbackId += 1
  return `message-fallback-${fallbackId}`
}
