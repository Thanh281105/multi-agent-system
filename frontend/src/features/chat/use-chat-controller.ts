import { useCallback, useEffect, useReducer, useRef, useState } from "react"

import {
  chatReducer,
  createInitialChatState,
  selectWorkspaceState,
  type ChatFailure,
  type ChatState,
} from "@/features/chat/chat-state"
import {
  DURABLE_CHAT_METADATA_KEY,
  clearChatSnapshot,
  clearDurableChatMetadata,
  readChatSnapshot,
  readDurableChatMetadata,
  writeChatSnapshot,
  writeDurableChatMetadata,
  type DurableChatStorageScope,
  type PendingTurnRecovery,
  type SessionStorageAdapter,
} from "@/features/chat/chat-storage"
import {
  createV2ApiClient,
  isAbortError as isV2AbortError,
  V2ApiError,
  type V2ApiClient,
} from "@/lib/v2-api"
import type {
  ActionCard,
  ActionReadResponse,
  ConversationMode,
  ConversationSummary,
  MeResponse,
  PreferenceRecord,
  PreferenceValue,
  TurnResponse,
  TurnSseEvent,
} from "@/lib/v2-contracts"
import { V2IncompleteStreamError, V2StreamError } from "@/lib/v2-stream"

export interface ChatControllerDependencies {
  storage?: SessionStorageAdapter | null
  online?: boolean
  v2Api?: V2ApiClient
  createClientTurnId?: () => string
  createIdempotencyKey?: () => string
  createStorageScopeId?: (identity: MeResponse) => string
}

export type SendOutcome =
  | "completed"
  | "failed"
  | "cancelled"
  | "credential_required"
  | "offline"
  | "busy"
  | "invalid_message"

export type DurableOperationOutcome =
  | "completed"
  | "failed"
  | "cancelled"
  | "credential_required"
  | "offline"
  | "busy"
  | "invalid_mode"
  | "not_ready"
  | "not_found"

export type DurableSendOutcome = SendOutcome | "not_ready"
export type DurableControllerPhase = "idle" | "loading" | "ready" | "failed"

export interface DurableChatController {
  identity: MeResponse | null
  allowedModes: ConversationMode[]
  selectedMode: ConversationMode | null
  conversations: ConversationSummary[]
  phase: DurableControllerPhase
  failure: ChatFailure | null
  bootstrap(preferredMode?: ConversationMode): Promise<DurableOperationOutcome>
  listConversations(mode?: ConversationMode): Promise<ConversationSummary[] | null>
  createConversation(mode?: ConversationMode): Promise<ConversationSummary | null>
  switchConversation(conversationId: string): Promise<DurableOperationOutcome>
  deleteConversation(conversationId: string): Promise<DurableOperationOutcome>
  changeMode(mode: ConversationMode): Promise<DurableOperationOutcome>
  sendMessage(message: string): Promise<DurableSendOutcome>
  retryPendingTurn(): Promise<DurableSendOutcome>
  cancelRequest(): Promise<TurnResponse | null>
  getAction(actionId: string): Promise<ActionReadResponse | null>
  confirmAction(actionId: string, proposalVersion: number): Promise<ActionReadResponse | null>
  rejectAction(
    actionId: string,
    proposalVersion: number,
    reason?: string,
  ): Promise<ActionReadResponse | null>
  listPreferences(): Promise<PreferenceRecord[] | null>
  putPreference(
    sourceTurnId: string,
    preference: PreferenceValue,
  ): Promise<PreferenceRecord | null>
  deletePreference(preferenceId: string): Promise<DurableOperationOutcome>
}

export interface ChatController {
  state: ChatState
  workspaceState: ReturnType<typeof selectWorkspaceState>
  storageAvailable: boolean
  configureCredential(value: string): boolean
  clearCredential(): void
  sendMessage(message: string): Promise<SendOutcome>
  cancelRequest(): void
  resetSession(): void
  durable?: DurableChatController
}

interface DurableControllerSnapshot {
  identity: MeResponse | null
  allowedModes: ConversationMode[]
  selectedMode: ConversationMode | null
  conversations: ConversationSummary[]
  phase: DurableControllerPhase
  failure: ChatFailure | null
}

interface ActiveTurnIdentity extends PendingTurnRecovery {
  turnId: string | null
  generation: number
  settled: boolean
  turnIdReady: Promise<string | null>
  resolveTurnId(turnId: string | null): void
}

const emptyDurableSnapshot: DurableControllerSnapshot = {
  identity: null,
  allowedModes: [],
  selectedMode: null,
  conversations: [],
  phase: "idle",
  failure: null,
}

export function useChatController(
  dependencies: ChatControllerDependencies = {},
): ChatController & { durable: DurableChatController } {
  const [runtime] = useState(() => ({
    storage:
      dependencies.storage === undefined
        ? resolveSessionStorage()
        : dependencies.storage,
    v2Api: dependencies.v2Api ?? createV2ApiClient(),
    createClientTurnId: dependencies.createClientTurnId ?? createClientTurnId,
    createIdempotencyKey:
      dependencies.createIdempotencyKey ?? createIdempotencyKey,
    createStorageScopeId:
      dependencies.createStorageScopeId ?? createStorageScopeId,
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
  const [durableSnapshot, setDurableSnapshot] = useState(emptyDurableSnapshot)
  const stateRef = useRef(state)
  const durableSnapshotRef = useRef(durableSnapshot)
  const credentialRef = useRef("")
  const durableControllersRef = useRef(new Set<AbortController>())
  const durableStreamRef = useRef<AbortController | null>(null)
  const activeTurnIdentityRef = useRef<ActiveTurnIdentity | null>(null)
  const durableScopeRef = useRef<DurableChatStorageScope | null>(null)
  const pendingRecoveryRef = useRef<PendingTurnRecovery | null>(null)
  const bootstrapPromiseRef = useRef<Promise<DurableOperationOutcome> | null>(null)
  const confirmKeysRef = useRef(new Map<string, string>())
  const confirmPromisesRef = useRef(
    new Map<string, Promise<ActionReadResponse | null>>(),
  )
  const durableGenerationRef = useRef(state.durable.generation)
  const [storageAvailable, setStorageAvailable] = useState(true)

  useEffect(() => {
    stateRef.current = state
  }, [state])

  useEffect(() => {
    durableSnapshotRef.current = durableSnapshot
  }, [durableSnapshot])

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
    if (dependencies.online !== undefined || typeof window === "undefined") return
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

  const updateDurableSnapshot = useCallback(
    (
      update:
        | DurableControllerSnapshot
        | ((current: DurableControllerSnapshot) => DurableControllerSnapshot),
    ) => {
      const current = durableSnapshotRef.current
      const next = typeof update === "function" ? update(current) : update
      durableSnapshotRef.current = next
      setDurableSnapshot(next)
    },
    [],
  )

  const nextDurableGeneration = useCallback(() => {
    const next = Math.max(
      durableGenerationRef.current,
      stateRef.current.durable.generation,
    ) + 1
    durableGenerationRef.current = next
    return next
  }, [])

  const abortDurableWork = useCallback(() => {
    for (const controller of durableControllersRef.current) controller.abort()
    durableControllersRef.current.clear()
    durableStreamRef.current = null
    bootstrapPromiseRef.current = null
  }, [])

  const registerDurableController = useCallback(() => {
    const controller = new AbortController()
    durableControllersRef.current.add(controller)
    return controller
  }, [])

  const releaseDurableController = useCallback((controller: AbortController) => {
    durableControllersRef.current.delete(controller)
    if (durableStreamRef.current === controller) durableStreamRef.current = null
  }, [])

  const clearDurableStorage = useCallback(() => {
    const storage = runtime.storage
    if (storage && !clearDurableChatMetadata(storage)) setStorageAvailable(false)
    pendingRecoveryRef.current = null
  }, [runtime.storage])

  const resetDurableScope = useCallback(
    (options: { clearStorage: boolean; phase?: DurableControllerPhase }) => {
      abortDurableWork()
      const generation = nextDurableGeneration()
      dispatch({ type: "durable.reset", generation })
      activeTurnIdentityRef.current = null
      confirmKeysRef.current.clear()
      confirmPromisesRef.current.clear()
      if (options.clearStorage) clearDurableStorage()
      durableScopeRef.current = null
      updateDurableSnapshot({
        ...emptyDurableSnapshot,
        phase: options.phase ?? "idle",
      })
    },
    [abortDurableWork, clearDurableStorage, nextDurableGeneration, updateDurableSnapshot],
  )

  useEffect(
    () => () => {
      abortDurableWork()
    },
    [abortDurableWork],
  )

  const configureCredential = useCallback(
    (value: string) => {
      const cleaned = value.trim()
      if (cleaned.length < 8) return false
      const replacingCredential = credentialRef.current.length > 0
      credentialRef.current = cleaned
      if (replacingCredential) resetDurableScope({ clearStorage: true })
      const generation = nextDurableGeneration()
      dispatch({ type: "credential.changed", configured: true })
      dispatch({ type: "durable.reset", generation })
      return true
    },
    [nextDurableGeneration, resetDurableScope],
  )

  const clearCredential = useCallback(() => {
    credentialRef.current = ""
    resetDurableScope({ clearStorage: true })
    const generation = nextDurableGeneration()
    dispatch({ type: "credential.changed", configured: false })
    dispatch({ type: "durable.reset", generation })
  }, [nextDurableGeneration, resetDurableScope])

  const persistDurableMetadata = useCallback(
    (
      selectedConversationId: string | null,
      pendingRecovery: PendingTurnRecovery | null,
    ) => {
      const storage = runtime.storage
      const scope = durableScopeRef.current
      pendingRecoveryRef.current = pendingRecovery
      if (!storage || !scope) return true
      const saved = writeDurableChatMetadata(storage, scope, {
        selectedConversationId,
        pendingRecovery,
      })
      if (!saved) setStorageAvailable(false)
      return saved
    },
    [runtime.storage],
  )

  const handleV2Failure = useCallback(
    (error: unknown): ChatFailure => {
      const failure = toV2ChatFailure(error)
      if (isAuthenticationFailure(error)) clearCredential()
      return failure
    },
    [clearCredential],
  )

  const hydrateConversation = useCallback(
    async (
      conversation: ConversationSummary,
      controller: AbortController,
      preloadedTurns?: Awaited<ReturnType<V2ApiClient["getConversation"]>>["turns"],
    ) => {
      const identity = durableSnapshotRef.current.identity
      const mode = durableScopeRef.current?.mode
      if (!identity || !mode || !isConversationAuthorized(conversation, identity, mode)) {
        return null
      }
      const generation = nextDurableGeneration()
      dispatch({ type: "durable.conversation.selected", generation, conversation })
      updateDurableSnapshot((current) => ({
        ...current,
        selectedMode: mode,
        phase: "loading",
        failure: null,
      }))
      try {
        const apiKey = credentialRef.current
        const [detail, preferenceResponse] = await Promise.all([
          preloadedTurns === undefined
            ? runtime.v2Api.getConversation({
                apiKey,
                signal: controller.signal,
                conversationId: conversation.conversationId,
              })
            : Promise.resolve({ conversation, turns: preloadedTurns }),
          runtime.v2Api.listPreferences({ apiKey, signal: controller.signal, mode }),
        ])
        if (controller.signal.aborted || generation !== durableGenerationRef.current) {
          return null
        }
        if (
          detail.conversation.conversationId !== conversation.conversationId ||
          !isConversationAuthorized(detail.conversation, identity, mode)
        ) {
          throw new V2ApiError(
            "v2.conversation_scope_mismatch",
            "Cuộc hội thoại không thuộc phạm vi đã xác thực.",
          )
        }
        dispatch({
          type: "durable.history.hydrated",
          generation,
          conversation: detail.conversation,
          turns: detail.turns,
          preferences: preferenceResponse.preferences,
        })
        persistDurableMetadata(
          detail.conversation.conversationId,
          pendingRecoveryRef.current?.conversationId === detail.conversation.conversationId
            ? pendingRecoveryRef.current
            : null,
        )
        updateDurableSnapshot((current) => ({
          ...current,
          selectedMode: mode,
          conversations: replaceConversation(current.conversations, detail.conversation),
          phase: "ready",
          failure: null,
        }))
        return { conversation: detail.conversation, turns: detail.turns }
      } catch (error) {
        if (
          controller.signal.aborted ||
          generation !== durableGenerationRef.current ||
          isV2AbortError(error)
        ) {
          return null
        }
        const failure = handleV2Failure(error)
        dispatch({ type: "durable.history.failed", generation, failure })
        updateDurableSnapshot((current) => ({ ...current, phase: "failed", failure }))
        return null
      }
    },
    [handleV2Failure, nextDurableGeneration, persistDurableMetadata, runtime.v2Api, updateDurableSnapshot],
  )

  const reconcileTurn = useCallback(
    (
      response: TurnResponse,
      turn: ActiveTurnIdentity,
      generation: number,
      actionType: "durable.turn.reconciled" | "durable.turn.cancelled" =
        "durable.turn.reconciled",
    ) => {
      if (!turnResponseMatches(response, turn)) {
        throw new V2ApiError(
          "v2.turn_correlation_mismatch",
          "Lượt trả lời không khớp yêu cầu đang xử lý.",
        )
      }
      dispatch({ type: actionType, generation, response })
      turn.turnId = response.turn.turnId
      turn.settled = isTerminalTurnResponse(response)
      if (turn.settled) {
        activeTurnIdentityRef.current = null
        persistDurableMetadata(turn.conversationId, null)
      }
      return response
    },
    [persistDurableMetadata],
  )

  const executeDurableTurn = useCallback(
    async (
      recovery: PendingTurnRecovery,
      options: { existing: boolean; turnId?: string | null },
    ): Promise<DurableSendOutcome> => {
      if (!credentialRef.current) return "credential_required"
      if (!stateRef.current.online) return "offline"
      if (durableStreamRef.current) return "busy"
      const selected = durableSnapshotRef.current.conversations.find(
        (conversation) => conversation.conversationId === recovery.conversationId,
      )
      if (!selected || durableSnapshotRef.current.phase !== "ready") {
        return "not_ready"
      }

      let generation = nextDurableGeneration()
      if (options.existing) {
        dispatch({ type: "durable.turn.retried", generation })
      } else {
        dispatch({
          type: "durable.turn.started",
          generation,
          conversationId: recovery.conversationId,
          clientTurnId: recovery.clientTurnId,
          message: recovery.message,
        })
      }
      const turnIdSignal = createTurnIdSignal(options.turnId ?? null)
      const turn: ActiveTurnIdentity = {
        ...recovery,
        turnId: options.turnId ?? null,
        generation,
        settled: false,
        turnIdReady: turnIdSignal.promise,
        resolveTurnId: turnIdSignal.resolve,
      }
      activeTurnIdentityRef.current = turn
      persistDurableMetadata(recovery.conversationId, recovery)

      try {
        for (let attempt = 0; attempt < 2; attempt += 1) {
        if (attempt > 0) {
          generation = nextDurableGeneration()
          turn.generation = generation
          dispatch({ type: "durable.turn.retried", generation })
        }
        const controller = registerDurableController()
        durableStreamRef.current = controller
        try {
          const terminal = await runtime.v2Api.streamChat({
            apiKey: credentialRef.current,
            signal: controller.signal,
            request: recovery,
            onEvent: (event) => {
              if (
                controller.signal.aborted ||
                generation !== durableGenerationRef.current ||
                turn.settled
              ) {
                return
              }
              if (turn.turnId !== null && event.turnId !== turn.turnId) return
              if (turn.turnId === null) {
                turn.turnId = event.turnId
                turn.resolveTurnId(event.turnId)
              }
              dispatch({ type: "durable.turn.event", generation, event })
              if (event.event === "terminal" && event.serverSettled) {
                turn.settled = true
              }
            },
          })
          if (
            controller.signal.aborted ||
            generation !== durableGenerationRef.current
          ) {
            return "cancelled"
          }
          if (turn.turnId !== null && terminal.turnId !== turn.turnId) {
            throw new V2ApiError(
              "v2.turn_correlation_mismatch",
              "Lượt trả lời không khớp yêu cầu đang xử lý.",
            )
          }
          if (turn.turnId === null) {
            turn.turnId = terminal.turnId
            turn.resolveTurnId(terminal.turnId)
          }
          dispatch({ type: "durable.turn.event", generation, event: terminal })
          turn.settled = terminal.serverSettled
          if (!turn.settled) {
            if (attempt === 1) return "failed"
            continue
          }
          activeTurnIdentityRef.current = null
          persistDurableMetadata(recovery.conversationId, null)
          return outcomeFromTerminal(terminal)
        } catch (error) {
          if (
            controller.signal.aborted ||
            generation !== durableGenerationRef.current ||
            isV2AbortError(error)
          ) {
            return "cancelled"
          }
          if (error instanceof V2IncompleteStreamError) {
            if (turn.turnId === null && error.turnId) {
              turn.turnId = error.turnId
              turn.resolveTurnId(error.turnId)
            }
          }
          if (!isRecoverableTurnFailure(error)) {
            const failure = handleV2Failure(error)
            dispatch({ type: "durable.turn.transport-failed", generation, failure })
            return "failed"
          }

          if (turn.turnId) {
            try {
              const response = await runtime.v2Api.getTurn({
                apiKey: credentialRef.current,
                signal: controller.signal,
                turnId: turn.turnId,
              })
              if (generation !== durableGenerationRef.current) return "cancelled"
              reconcileTurn(response, turn, generation)
              if (turn.settled) return outcomeFromTurnResponse(response)
            } catch (readError) {
              if (
                controller.signal.aborted ||
                generation !== durableGenerationRef.current ||
                isV2AbortError(readError)
              ) {
                return "cancelled"
              }
              if (!isRecoverableTurnFailure(readError)) {
                const failure = handleV2Failure(readError)
                dispatch({ type: "durable.turn.transport-failed", generation, failure })
                return "failed"
              }
            }
          }
          } finally {
            releaseDurableController(controller)
          }
        }

        const failure: ChatFailure = {
          source: "client",
          code: "v2.turn_recovery_pending",
          message: "Kết nối bị gián đoạn; lượt trả lời có thể được khôi phục.",
          retryable: true,
        }
        dispatch({ type: "durable.turn.transport-failed", generation, failure })
        return "failed"
      } finally {
        turn.resolveTurnId(turn.turnId)
      }
    },
    [handleV2Failure, nextDurableGeneration, persistDurableMetadata, reconcileTurn, registerDurableController, releaseDurableController, runtime.v2Api],
  )

  const recoverPendingTurn = useCallback(
    async (
      recovery: PendingTurnRecovery,
      turns: Awaited<ReturnType<V2ApiClient["getConversation"]>>["turns"],
    ) => {
      const storedTurn = turns.find(
        (turn) => turn.clientTurnId === recovery.clientTurnId,
      )
      if (storedTurn && !isLiveStatus(storedTurn.status)) {
        persistDurableMetadata(recovery.conversationId, null)
        return outcomeFromStatus(storedTurn.status)
      }
      return executeDurableTurn(recovery, {
        existing: Boolean(storedTurn),
        turnId: storedTurn?.turnId,
      })
    },
    [executeDurableTurn, persistDurableMetadata],
  )

  const bootstrap = useCallback(
    (preferredMode?: ConversationMode): Promise<DurableOperationOutcome> => {
      if (!credentialRef.current) return Promise.resolve("credential_required")
      if (!stateRef.current.online) return Promise.resolve("offline")
      if (bootstrapPromiseRef.current) return bootstrapPromiseRef.current

      const operation = (async (): Promise<DurableOperationOutcome> => {
        abortDurableWork()
        const generation = nextDurableGeneration()
        dispatch({ type: "durable.history.loading", generation })
        updateDurableSnapshot({ ...emptyDurableSnapshot, phase: "loading" })
        const controller = registerDurableController()
        try {
          const identity = await runtime.v2Api.getMe({
            apiKey: credentialRef.current,
            signal: controller.signal,
          })
          if (
            controller.signal.aborted ||
            generation !== durableGenerationRef.current
          ) {
            return "cancelled"
          }
          if (identity.allowedModes.length === 0) {
            throw new V2ApiError(
              "v2.no_allowed_mode",
              "Tài khoản chưa được cấp chế độ hội thoại.",
            )
          }
          if (preferredMode && !identity.allowedModes.includes(preferredMode)) {
            updateDurableSnapshot({
              identity,
              allowedModes: [...identity.allowedModes],
              selectedMode: null,
              conversations: [],
              phase: "failed",
              failure: invalidModeFailure(),
            })
            return "invalid_mode"
          }

          const scopeId = runtime.createStorageScopeId(identity)
          const rememberedMode = readRememberedMode(
            runtime.storage,
            identity.allowedModes,
          )
          const mode = preferredMode ?? rememberedMode ?? identity.allowedModes[0]
          const previousScope = durableScopeRef.current
          if (previousScope && previousScope.scopeId !== scopeId) {
            clearDurableStorage()
          }
          durableScopeRef.current = { scopeId, mode }
          const metadata = runtime.storage
            ? readDurableChatMetadata(runtime.storage, { scopeId, mode })
            : {
                version: 2 as const,
                selectedMode: mode,
                selectedConversationId: null,
                pendingRecovery: null,
              }
          pendingRecoveryRef.current = metadata.pendingRecovery

          const listed = await runtime.v2Api.listConversations({
            apiKey: credentialRef.current,
            signal: controller.signal,
            mode,
          })
          if (
            controller.signal.aborted ||
            generation !== durableGenerationRef.current
          ) {
            return "cancelled"
          }
          assertConversationListAuthorized(listed.conversations, identity, mode)
          let conversations = [...listed.conversations]
          let selected = metadata.selectedConversationId
            ? conversations.find(
                (item) => item.conversationId === metadata.selectedConversationId,
              )
            : undefined
          let preloadedTurns:
            | Awaited<ReturnType<V2ApiClient["getConversation"]>>["turns"]
            | undefined

          if (!selected && metadata.selectedConversationId) {
            try {
              const detail = await runtime.v2Api.getConversation({
                apiKey: credentialRef.current,
                signal: controller.signal,
                conversationId: metadata.selectedConversationId,
              })
              if (!isConversationAuthorized(detail.conversation, identity, mode)) {
                throw new V2ApiError(
                  "v2.conversation_scope_mismatch",
                  "Cuộc hội thoại không thuộc phạm vi đã xác thực.",
                )
              }
              selected = detail.conversation
              preloadedTurns = detail.turns
              conversations = replaceConversation(conversations, selected)
            } catch (error) {
              if (!isMissingResource(error)) throw error
              pendingRecoveryRef.current = null
            }
          }
          selected ??= conversations[0]
          if (!selected) {
            const created = await runtime.v2Api.createConversation({
              apiKey: credentialRef.current,
              signal: controller.signal,
              mode,
            })
            if (!isConversationAuthorized(created.conversation, identity, mode)) {
              throw new V2ApiError(
                "v2.conversation_scope_mismatch",
                "Cuộc hội thoại không thuộc phạm vi đã xác thực.",
              )
            }
            selected = created.conversation
            conversations = [selected]
          }
          updateDurableSnapshot({
            identity,
            allowedModes: [...identity.allowedModes],
            selectedMode: mode,
            conversations,
            phase: "loading",
            failure: null,
          })
          const hydrated = await hydrateConversation(selected, controller, preloadedTurns)
          if (!hydrated) {
            return controller.signal.aborted ? "cancelled" : "failed"
          }
          const recovery = pendingRecoveryRef.current
          if (recovery?.conversationId === selected.conversationId) {
            const recovered = await recoverPendingTurn(recovery, hydrated.turns)
            return recovered === "completed" ||
              recovered === "cancelled" ||
              recovered === "failed"
              ? recovered
              : "failed"
          }
          return "completed"
        } catch (error) {
          if (controller.signal.aborted || isV2AbortError(error)) return "cancelled"
          const failure = handleV2Failure(error)
          if (generation === durableGenerationRef.current) {
            dispatch({ type: "durable.history.failed", generation, failure })
            updateDurableSnapshot((current) => ({
              ...current,
              phase: "failed",
              failure,
            }))
          }
          return "failed"
        } finally {
          releaseDurableController(controller)
        }
      })()
      bootstrapPromiseRef.current = operation
      void operation.finally(() => {
        if (bootstrapPromiseRef.current === operation) bootstrapPromiseRef.current = null
      })
      return operation
    },
    [abortDurableWork, clearDurableStorage, handleV2Failure, hydrateConversation, nextDurableGeneration, recoverPendingTurn, registerDurableController, releaseDurableController, runtime, updateDurableSnapshot],
  )

  const listConversations = useCallback(
    async (requestedMode?: ConversationMode) => {
      const identity = durableSnapshotRef.current.identity
      const mode = requestedMode ?? durableSnapshotRef.current.selectedMode
      if (
        !credentialRef.current ||
        !identity ||
        !mode ||
        !identity.allowedModes.includes(mode) ||
        !stateRef.current.online
      ) {
        return null
      }
      const generation = durableGenerationRef.current
      const controller = registerDurableController()
      try {
        const response = await runtime.v2Api.listConversations({
          apiKey: credentialRef.current,
          signal: controller.signal,
          mode,
        })
        if (controller.signal.aborted || generation !== durableGenerationRef.current) {
          return null
        }
        assertConversationListAuthorized(response.conversations, identity, mode)
        if (mode === durableSnapshotRef.current.selectedMode) {
          updateDurableSnapshot((current) => ({
            ...current,
            conversations: [...response.conversations],
            failure: null,
          }))
        }
        return [...response.conversations]
      } catch (error) {
        if (!isV2AbortError(error)) handleV2Failure(error)
        return null
      } finally {
        releaseDurableController(controller)
      }
    },
    [handleV2Failure, registerDurableController, releaseDurableController, runtime.v2Api, updateDurableSnapshot],
  )

  const loadMode = useCallback(
    async (
      mode: ConversationMode,
      forceCreate: boolean,
    ): Promise<ConversationSummary | null> => {
      const identity = durableSnapshotRef.current.identity
      if (!credentialRef.current || !identity || !stateRef.current.online) return null
      if (!identity.allowedModes.includes(mode)) return null

      abortDurableWork()
      const modeChanged = durableScopeRef.current?.mode !== mode
      if (modeChanged) clearDurableStorage()
      const scopeId = runtime.createStorageScopeId(identity)
      durableScopeRef.current = { scopeId, mode }
      const resetGeneration = nextDurableGeneration()
      dispatch({ type: "durable.reset", generation: resetGeneration })
      activeTurnIdentityRef.current = null
      pendingRecoveryRef.current = null
      const controller = registerDurableController()
      try {
        const response = await runtime.v2Api.listConversations({
          apiKey: credentialRef.current,
          signal: controller.signal,
          mode,
        })
        assertConversationListAuthorized(response.conversations, identity, mode)
        let conversations = [...response.conversations]
        let selected = conversations[0]
        if (forceCreate || !selected) {
          const created = await runtime.v2Api.createConversation({
            apiKey: credentialRef.current,
            signal: controller.signal,
            mode,
          })
          if (!isConversationAuthorized(created.conversation, identity, mode)) {
            throw new V2ApiError(
              "v2.conversation_scope_mismatch",
              "Cuộc hội thoại không thuộc phạm vi đã xác thực.",
            )
          }
          selected = created.conversation
          conversations = replaceConversation(conversations, selected)
        }
        updateDurableSnapshot({
          identity,
          allowedModes: [...identity.allowedModes],
          selectedMode: mode,
          conversations,
          phase: "loading",
          failure: null,
        })
        const hydrated = await hydrateConversation(selected, controller)
        return hydrated?.conversation ?? null
      } catch (error) {
        if (!controller.signal.aborted && !isV2AbortError(error)) {
          const failure = handleV2Failure(error)
          updateDurableSnapshot((current) => ({ ...current, phase: "failed", failure }))
        }
        return null
      } finally {
        releaseDurableController(controller)
      }
    },
    [abortDurableWork, clearDurableStorage, handleV2Failure, hydrateConversation, nextDurableGeneration, registerDurableController, releaseDurableController, runtime, updateDurableSnapshot],
  )

  const changeMode = useCallback(
    async (mode: ConversationMode): Promise<DurableOperationOutcome> => {
      if (!credentialRef.current) return "credential_required"
      if (!stateRef.current.online) return "offline"
      const identity = durableSnapshotRef.current.identity
      if (!identity) return "not_ready"
      if (!identity.allowedModes.includes(mode)) return "invalid_mode"
      if (durableStreamRef.current && durableScopeRef.current?.mode === mode) {
        return "busy"
      }
      return (await loadMode(mode, false)) ? "completed" : "failed"
    },
    [loadMode],
  )

  const createConversation = useCallback(
    async (mode = durableSnapshotRef.current.selectedMode ?? undefined) => {
      if (!mode) return null
      return loadMode(mode, true)
    },
    [loadMode],
  )

  const switchConversation = useCallback(
    async (conversationId: string): Promise<DurableOperationOutcome> => {
      if (!credentialRef.current) return "credential_required"
      if (!stateRef.current.online) return "offline"
      const snapshot = durableSnapshotRef.current
      const conversation = snapshot.conversations.find(
        (item) => item.conversationId === conversationId,
      )
      if (!conversation) return "not_found"
      const identity = snapshot.identity
      const mode = snapshot.selectedMode
      if (!identity || !mode) return "not_ready"
      if (!isConversationAuthorized(conversation, identity, mode)) return "not_found"
      abortDurableWork()
      const resetGeneration = nextDurableGeneration()
      dispatch({ type: "durable.reset", generation: resetGeneration })
      activeTurnIdentityRef.current = null
      pendingRecoveryRef.current = null
      const controller = registerDurableController()
      try {
        return (await hydrateConversation(conversation, controller))
          ? "completed"
          : controller.signal.aborted
            ? "cancelled"
            : "failed"
      } finally {
        releaseDurableController(controller)
      }
    },
    [abortDurableWork, hydrateConversation, nextDurableGeneration, registerDurableController, releaseDurableController],
  )

  const deleteConversation = useCallback(
    async (conversationId: string): Promise<DurableOperationOutcome> => {
      if (!credentialRef.current) return "credential_required"
      if (!stateRef.current.online) return "offline"
      const snapshot = durableSnapshotRef.current
      const conversation = snapshot.conversations.find(
        (item) => item.conversationId === conversationId,
      )
      if (!conversation) return "not_found"
      const controller = registerDurableController()
      const generation = durableGenerationRef.current
      try {
        await runtime.v2Api.deleteConversation({
          apiKey: credentialRef.current,
          signal: controller.signal,
          conversationId,
        })
        if (controller.signal.aborted || generation !== durableGenerationRef.current) {
          return "cancelled"
        }
        const remaining = durableSnapshotRef.current.conversations.filter(
          (item) => item.conversationId !== conversationId,
        )
        updateDurableSnapshot((current) => ({ ...current, conversations: remaining }))
        if (stateRef.current.durable.activeConversation?.conversationId !== conversationId) {
          return "completed"
        }
        const mode = durableSnapshotRef.current.selectedMode
        if (!mode) return "not_ready"
        if (remaining[0]) return await switchConversation(remaining[0].conversationId)
        return (await loadMode(mode, true)) ? "completed" : "failed"
      } catch (error) {
        if (controller.signal.aborted || isV2AbortError(error)) return "cancelled"
        handleV2Failure(error)
        return "failed"
      } finally {
        releaseDurableController(controller)
      }
    },
    [handleV2Failure, loadMode, registerDurableController, releaseDurableController, runtime.v2Api, switchConversation, updateDurableSnapshot],
  )

  const sendDurableMessage = useCallback(
    async (message: string): Promise<DurableSendOutcome> => {
      const cleaned = message.trim()
      if (!credentialRef.current) return "credential_required"
      if (!stateRef.current.online) return "offline"
      if (durableStreamRef.current) return "busy"
      if (
        (activeTurnIdentityRef.current && !activeTurnIdentityRef.current.settled) ||
        (stateRef.current.durable.activeTurn &&
          !stateRef.current.durable.activeTurn.serverSettled)
      ) {
        return "busy"
      }
      if (cleaned.length === 0 || cleaned.length > 2_000) return "invalid_message"
      const conversation = stateRef.current.durable.activeConversation
      const snapshot = durableSnapshotRef.current
      if (
        !conversation ||
        snapshot.phase !== "ready" ||
        conversation.mode !== snapshot.selectedMode
      ) {
        return "not_ready"
      }
      return executeDurableTurn(
        {
          conversationId: conversation.conversationId,
          clientTurnId: runtime.createClientTurnId(),
          message: cleaned,
        },
        { existing: false },
      )
    },
    [executeDurableTurn, runtime],
  )

  const retryPendingTurn = useCallback(async (): Promise<DurableSendOutcome> => {
    const active = stateRef.current.durable.activeTurn
    const recovery = pendingRecoveryRef.current ??
      (active && !active.serverSettled
        ? {
            conversationId: active.conversationId,
            clientTurnId: active.clientTurnId,
            message: active.message,
          }
        : null)
    if (!recovery) return "not_ready"
    return executeDurableTurn(recovery, {
      existing: Boolean(active),
      turnId: active?.turnId,
    })
  }, [executeDurableTurn])

  const cancelDurableRequest = useCallback(async (): Promise<TurnResponse | null> => {
    const active = activeTurnIdentityRef.current
    const reducerActive = stateRef.current.durable.activeTurn
    if ((!active && !reducerActive) || active?.settled) return null
    const fallbackSignal = createTurnIdSignal(reducerActive?.turnId ?? null)
    const turn: ActiveTurnIdentity = active ?? {
        conversationId: reducerActive!.conversationId,
        clientTurnId: reducerActive!.clientTurnId,
        message: reducerActive!.message,
        turnId: reducerActive!.turnId,
        generation: reducerActive!.generation,
        settled: reducerActive!.serverSettled,
        turnIdReady: fallbackSignal.promise,
        resolveTurnId: fallbackSignal.resolve,
    }
    if (turn.settled) return null

    dispatch({
      type: "durable.turn.cancel.requested",
      generation: turn.generation,
    })
    const admittedTurnId = turn.turnId ?? await turn.turnIdReady
    if (!admittedTurnId || turn.settled) return null
    const generation = nextDurableGeneration()
    const cancelledTurn: ActiveTurnIdentity = {
      ...turn,
      turnId: admittedTurnId,
      generation,
    }
    activeTurnIdentityRef.current = cancelledTurn
    dispatch({ type: "durable.turn.retried", generation })
    dispatch({ type: "durable.turn.cancel.requested", generation })
    durableStreamRef.current?.abort()
    durableStreamRef.current = null
    if (!credentialRef.current) return null

    const controller = registerDurableController()
    try {
      const response = await runtime.v2Api.cancelTurn({
        apiKey: credentialRef.current,
        signal: controller.signal,
        turnId: admittedTurnId,
      })
      if (controller.signal.aborted || generation !== durableGenerationRef.current) return null
      return reconcileTurn(
        response,
        cancelledTurn,
        generation,
        "durable.turn.cancelled",
      )
    } catch (error) {
      if (
        controller.signal.aborted ||
        generation !== durableGenerationRef.current ||
        isV2AbortError(error)
      ) {
        return null
      }
      try {
        const response = await runtime.v2Api.getTurn({
          apiKey: credentialRef.current,
          signal: controller.signal,
          turnId: admittedTurnId,
        })
        if (generation !== durableGenerationRef.current) return null
        return reconcileTurn(
          response,
          cancelledTurn,
          generation,
          "durable.turn.cancelled",
        )
      } catch (readError) {
        if (!isV2AbortError(readError)) {
          const failure = handleV2Failure(readError)
          dispatch({ type: "durable.turn.transport-failed", generation, failure })
        }
        return null
      }
    } finally {
      releaseDurableController(controller)
    }
  }, [handleV2Failure, nextDurableGeneration, reconcileTurn, registerDurableController, releaseDurableController, runtime.v2Api])

  const getAction = useCallback(
    async (actionId: string): Promise<ActionReadResponse | null> => {
      if (!credentialRef.current || !stateRef.current.online) return null
      if (!stateRef.current.durable.resources.actions[actionId]) return null
      const generation = durableGenerationRef.current
      const controller = registerDurableController()
      try {
        const response = await runtime.v2Api.getAction({
          apiKey: credentialRef.current,
          signal: controller.signal,
          actionId,
        })
        if (
          controller.signal.aborted ||
          generation !== durableGenerationRef.current ||
          response.action.actionId !== actionId
        ) {
          return null
        }
        dispatch({ type: "durable.action.updated", action: response.action })
        return response
      } catch (error) {
        if (!isV2AbortError(error)) handleV2Failure(error)
        return null
      } finally {
        releaseDurableController(controller)
      }
    },
    [handleV2Failure, registerDurableController, releaseDurableController, runtime.v2Api],
  )

  const confirmAction = useCallback(
    (actionId: string, proposalVersion: number) => {
      const intent = `${actionId}:${proposalVersion}`
      const existingPromise = confirmPromisesRef.current.get(intent)
      if (existingPromise) return existingPromise
      const operation = (async (): Promise<ActionReadResponse | null> => {
        if (!credentialRef.current || !stateRef.current.online) return null
        const card = stateRef.current.durable.resources.actions[actionId]
        if (!card || card.proposalVersion !== proposalVersion) return null
        if (isTerminalAction(card)) return getAction(actionId)
        const generation = durableGenerationRef.current
        const idempotencyKey =
          confirmKeysRef.current.get(intent) ?? runtime.createIdempotencyKey()
        confirmKeysRef.current.set(intent, idempotencyKey)
        const controller = registerDurableController()
        try {
          let execution
          try {
            execution = await runtime.v2Api.confirmAction({
              apiKey: credentialRef.current,
              signal: controller.signal,
              actionId,
              request: { proposalVersion },
              idempotencyKey,
            })
          } catch (error) {
            if (!isRecoverableTurnFailure(error)) throw error
            try {
              const readback = await runtime.v2Api.getAction({
                apiKey: credentialRef.current,
                signal: controller.signal,
                actionId,
              })
              if (generation !== durableGenerationRef.current) return null
              if (readback.action.actionId !== actionId) return null
              dispatch({ type: "durable.action.updated", action: readback.action })
              if (isTerminalAction(readback.action)) {
                confirmKeysRef.current.delete(intent)
                return readback
              }
            } catch (readError) {
              if (!isRecoverableTurnFailure(readError)) throw readError
            }
            execution = await runtime.v2Api.confirmAction({
              apiKey: credentialRef.current,
              signal: controller.signal,
              actionId,
              request: { proposalVersion },
              idempotencyKey,
            })
          }
          if (controller.signal.aborted || generation !== durableGenerationRef.current) {
            return null
          }
          dispatch({
            type: "durable.action.updated",
            action: { ...card, status: execution.status },
          })
          const readback = await runtime.v2Api.getAction({
            apiKey: credentialRef.current,
            signal: controller.signal,
            actionId,
          })
          if (
            controller.signal.aborted ||
            generation !== durableGenerationRef.current ||
            readback.action.actionId !== actionId
          ) {
            return null
          }
          dispatch({ type: "durable.action.updated", action: readback.action })
          if (isTerminalAction(readback.action)) confirmKeysRef.current.delete(intent)
          return readback
        } catch (error) {
          if (!isV2AbortError(error)) handleV2Failure(error)
          return null
        } finally {
          releaseDurableController(controller)
        }
      })()
      confirmPromisesRef.current.set(intent, operation)
      void operation.finally(() => {
        if (confirmPromisesRef.current.get(intent) === operation) {
          confirmPromisesRef.current.delete(intent)
        }
      })
      return operation
    },
    [getAction, handleV2Failure, registerDurableController, releaseDurableController, runtime],
  )

  const rejectAction = useCallback(
    async (
      actionId: string,
      proposalVersion: number,
      reason?: string,
    ): Promise<ActionReadResponse | null> => {
      if (!credentialRef.current || !stateRef.current.online) return null
      const card = stateRef.current.durable.resources.actions[actionId]
      if (!card || card.proposalVersion !== proposalVersion) return null
      if (isTerminalAction(card)) {
        const readback = await getAction(actionId)
        return readback?.action.status === "rejected" ? readback : null
      }
      const generation = durableGenerationRef.current
      const controller = registerDurableController()
      try {
        const decision = await runtime.v2Api.rejectAction({
          apiKey: credentialRef.current,
          signal: controller.signal,
          actionId,
          request: { proposalVersion, ...(reason === undefined ? {} : { reason }) },
        })
        if (controller.signal.aborted || generation !== durableGenerationRef.current) {
          return null
        }
        dispatch({
          type: "durable.action.updated",
          action: { ...card, status: decision.status },
        })
        const readback = await getAction(actionId)
        return readback?.action.status === "rejected" ? readback : null
      } catch (error) {
        if (isRecoverableTurnFailure(error)) {
          const readback = await getAction(actionId)
          if (readback?.action.status === "rejected") return readback
        }
        if (!isV2AbortError(error)) handleV2Failure(error)
        return null
      } finally {
        releaseDurableController(controller)
      }
    },
    [getAction, handleV2Failure, registerDurableController, releaseDurableController, runtime.v2Api],
  )

  const listPreferences = useCallback(async (): Promise<PreferenceRecord[] | null> => {
    const mode = durableSnapshotRef.current.selectedMode
    if (!credentialRef.current || !stateRef.current.online || !mode) return null
    const generation = durableGenerationRef.current
    const controller = registerDurableController()
    try {
      const response = await runtime.v2Api.listPreferences({
        apiKey: credentialRef.current,
        signal: controller.signal,
        mode,
      })
      if (controller.signal.aborted || generation !== durableGenerationRef.current) {
        return null
      }
      dispatch({
        type: "durable.preferences.hydrated",
        preferences: response.preferences,
      })
      return response.preferences
    } catch (error) {
      if (!isV2AbortError(error)) handleV2Failure(error)
      return null
    } finally {
      releaseDurableController(controller)
    }
  }, [handleV2Failure, registerDurableController, releaseDurableController, runtime.v2Api])

  const putPreference = useCallback(
    async (
      sourceTurnId: string,
      preference: PreferenceValue,
    ): Promise<PreferenceRecord | null> => {
      if (!isCompletedSourceTurn(stateRef.current, sourceTurnId)) return null
      if (!credentialRef.current || !stateRef.current.online) return null
      const generation = durableGenerationRef.current
      const controller = registerDurableController()
      try {
        const record = await runtime.v2Api.putPreference({
          apiKey: credentialRef.current,
          signal: controller.signal,
          request: { sourceTurnId, preference },
        })
        if (
          controller.signal.aborted ||
          generation !== durableGenerationRef.current ||
          record.sourceTurnId !== sourceTurnId
        ) {
          return null
        }
        return (await listPreferences()) ? record : null
      } catch (error) {
        if (!isV2AbortError(error)) handleV2Failure(error)
        return null
      } finally {
        releaseDurableController(controller)
      }
    },
    [handleV2Failure, listPreferences, registerDurableController, releaseDurableController, runtime.v2Api],
  )

  const deletePreference = useCallback(
    async (preferenceId: string): Promise<DurableOperationOutcome> => {
      const mode = durableSnapshotRef.current.selectedMode
      if (!credentialRef.current) return "credential_required"
      if (!stateRef.current.online) return "offline"
      if (!mode) return "not_ready"
      if (!stateRef.current.durable.resources.preferences[preferenceId]) {
        return "not_found"
      }
      const generation = durableGenerationRef.current
      const controller = registerDurableController()
      try {
        await runtime.v2Api.deletePreference({
          apiKey: credentialRef.current,
          signal: controller.signal,
          mode,
          request: { preferenceId },
        })
        if (controller.signal.aborted || generation !== durableGenerationRef.current) {
          return "cancelled"
        }
        return (await listPreferences()) ? "completed" : "failed"
      } catch (error) {
        if (controller.signal.aborted || isV2AbortError(error)) return "cancelled"
        handleV2Failure(error)
        return "failed"
      } finally {
        releaseDurableController(controller)
      }
    },
    [handleV2Failure, listPreferences, registerDurableController, releaseDurableController, runtime.v2Api],
  )

  const sendMessage = useCallback(
    async (message: string): Promise<SendOutcome> => {
      const outcome = await sendDurableMessage(message)
      return outcome === "not_ready" ? "failed" : outcome
    },
    [sendDurableMessage],
  )

  const cancelRequest = useCallback(() => {
    void cancelDurableRequest()
  }, [cancelDurableRequest])

  const resetSession = useCallback(() => {
    dispatch({
      type: "session.reset",
      generation: stateRef.current.request.generation + 1,
    })
    const storage = runtime.storage
    if (storage && !clearChatSnapshot(storage)) setStorageAvailable(false)
    if (durableSnapshotRef.current.phase === "ready") {
      void createConversation()
    }
  }, [createConversation, runtime.storage])

  return {
    state,
    workspaceState: selectWorkspaceState(state),
    storageAvailable,
    configureCredential,
    clearCredential,
    sendMessage,
    cancelRequest,
    resetSession,
    durable: {
      identity: durableSnapshot.identity,
      allowedModes: durableSnapshot.allowedModes,
      selectedMode: durableSnapshot.selectedMode,
      conversations: durableSnapshot.conversations,
      phase: durableSnapshot.phase,
      failure: durableSnapshot.failure,
      bootstrap,
      listConversations,
      createConversation,
      switchConversation,
      deleteConversation,
      changeMode,
      sendMessage: sendDurableMessage,
      retryPendingTurn,
      cancelRequest: cancelDurableRequest,
      getAction,
      confirmAction,
      rejectAction,
      listPreferences,
      putPreference,
      deletePreference,
    },
  }
}

function toV2ChatFailure(error: unknown): ChatFailure {
  if (error instanceof V2ApiError) {
    return {
      source: error.source === "server" ? "gateway" : "client",
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      ...(error.requestId ? { requestId: error.requestId } : {}),
      ...(error.traceId ? { traceId: error.traceId } : {}),
    }
  }
  if (error instanceof V2IncompleteStreamError || error instanceof V2StreamError) {
    return {
      source: "client",
      code: error.code,
      message: error.message,
      retryable: error instanceof V2IncompleteStreamError,
      ...(error.requestId ? { requestId: error.requestId } : {}),
      ...(error.traceId ? { traceId: error.traceId } : {}),
    }
  }
  return {
    source: "client",
    code: "v2.unknown_client_error",
    message: "Không thể xử lý yêu cầu lúc này.",
    retryable: false,
  }
}

function invalidModeFailure(): ChatFailure {
  return {
    source: "client",
    code: "v2.mode_not_allowed",
    message: "Tài khoản không được cấp chế độ này.",
    retryable: false,
  }
}

function isAuthenticationFailure(error: unknown): boolean {
  return error instanceof V2ApiError &&
    (error.httpStatus === 401 ||
      error.httpStatus === 403 ||
      error.code.includes("authentication"))
}

function isRecoverableTurnFailure(error: unknown): boolean {
  return error instanceof V2IncompleteStreamError ||
    (error instanceof V2ApiError && error.retryable)
}

function isMissingResource(error: unknown): boolean {
  return error instanceof V2ApiError && error.httpStatus === 404
}

function isConversationAuthorized(
  conversation: ConversationSummary,
  identity: MeResponse,
  mode: ConversationMode,
): boolean {
  return conversation.mode === mode && conversation.storeId === identity.storeId
}

function assertConversationListAuthorized(
  conversations: ConversationSummary[],
  identity: MeResponse,
  mode: ConversationMode,
): void {
  if (conversations.every((item) => isConversationAuthorized(item, identity, mode))) {
    return
  }
  throw new V2ApiError(
    "v2.conversation_scope_mismatch",
    "Danh sách hội thoại không thuộc phạm vi đã xác thực.",
  )
}

function replaceConversation(
  conversations: ConversationSummary[],
  conversation: ConversationSummary,
): ConversationSummary[] {
  return [
    conversation,
    ...conversations.filter(
      (item) => item.conversationId !== conversation.conversationId,
    ),
  ]
}

function turnResponseMatches(
  response: TurnResponse,
  turn: ActiveTurnIdentity,
): boolean {
  return response.conversationId === turn.conversationId &&
    response.turn.clientTurnId === turn.clientTurnId &&
    (turn.turnId === null || response.turn.turnId === turn.turnId)
}

function isTerminalTurnResponse(response: TurnResponse): boolean {
  return !isLiveStatus(response.turn.status)
}

function isLiveStatus(status: string): boolean {
  return status === "pending" || status === "running"
}

function outcomeFromStatus(status: string): DurableSendOutcome {
  if (status === "completed") return "completed"
  if (status === "cancelled") return "cancelled"
  return "failed"
}

function outcomeFromTurnResponse(response: TurnResponse): DurableSendOutcome {
  return outcomeFromStatus(response.turn.status)
}

function outcomeFromTerminal(
  terminal: Extract<TurnSseEvent, { event: "terminal" }>,
): DurableSendOutcome {
  return outcomeFromStatus(terminal.payload.status)
}

function isTerminalAction(action: ActionCard): boolean {
  return ["rejected", "executed", "expired", "conflicted", "failed"].includes(
    action.status,
  )
}

function createTurnIdSignal(initialTurnId: string | null): {
  promise: Promise<string | null>
  resolve(turnId: string | null): void
} {
  let settled = false
  let settle!: (turnId: string | null) => void
  const promise = new Promise<string | null>((resolve) => {
    settle = resolve
  })
  const resolve = (turnId: string | null) => {
    if (settled) return
    settled = true
    settle(turnId)
  }
  if (initialTurnId !== null) resolve(initialTurnId)
  return { promise, resolve }
}

function isCompletedSourceTurn(state: ChatState, turnId: string): boolean {
  return state.durable.history.some(
    (turn) => turn.turnId === turnId && turn.status === "completed",
  ) || Boolean(
    state.durable.activeTurn?.turnId === turnId &&
      state.durable.activeTurn.status === "completed" &&
      state.durable.activeTurn.serverSettled,
  )
}

function readRememberedMode(
  storage: SessionStorageAdapter | null,
  allowedModes: ConversationMode[],
): ConversationMode | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(DURABLE_CHAT_METADATA_KEY)
    if (!raw) return null
    const value: unknown = JSON.parse(raw)
    if (
      typeof value === "object" &&
      value !== null &&
      "selectedMode" in value &&
      (value.selectedMode === "shopper" || value.selectedMode === "merchant") &&
      allowedModes.includes(value.selectedMode)
    ) {
      return value.selectedMode
    }
  } catch {
    return null
  }
  return null
}

function resolveSessionStorage(): SessionStorageAdapter | null {
  if (typeof window === "undefined") return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function createStorageScopeId(identity: MeResponse): string {
  const value = `${identity.tenantId}\u0000${identity.principalId}\u0000${identity.storeId}`
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index)
    first = Math.imul(first ^ code, 0x01000193)
    second = Math.imul(second ^ code, 0x85ebca6b)
  }
  return `account-${(first >>> 0).toString(36)}-${(second >>> 0).toString(36)}`
}

let fallbackId = 0
function createRandomId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}:${crypto.randomUUID()}`
  }
  fallbackId += 1
  return `${prefix}:fallback-${fallbackId}`
}

function createClientTurnId(): string {
  return createRandomId("turn")
}

function createIdempotencyKey(): string {
  return createRandomId("confirm")
}
