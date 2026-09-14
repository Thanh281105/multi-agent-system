import type {
  GatewayChatResponse,
  GatewayStatusEvent,
  GatewayTokenEvent,
} from "@/lib/contracts"
import type {
  ActionCard,
  ConversationSummary,
  HistoryTurn,
  PreferenceRecord,
  TurnArtifact,
  TurnResponse,
  TurnResult,
  TurnSseEvent,
  TurnSseProgressEvent,
} from "@/lib/v2-contracts"

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

export type DurableTurnStatus = HistoryTurn["status"]
export type DurableDialogueOutcome = NonNullable<HistoryTurn["outcome"]>
export type DurableTerminalEvent = Extract<TurnSseEvent, { event: "terminal" }>

export interface DurableTurnLinks {
  artifactIds: string[]
  actionIds: string[]
  preferenceIds: string[]
}

export interface DurableResourceState {
  artifacts: Record<string, TurnArtifact>
  actions: Record<string, ActionCard>
  preferences: Record<string, PreferenceRecord>
  byTurnId: Record<string, DurableTurnLinks>
}

export interface DurableActiveTurn {
  generation: number
  conversationId: string
  turnId: string | null
  clientTurnId: string
  message: string
  status: DurableTurnStatus
  progress: TurnSseProgressEvent[]
  streamedAnswer: string
  lastSequence: number
  terminal: DurableTerminalEvent | null
  cancellationPending: boolean
  serverSettled: boolean
}

export interface DurableChatState {
  generation: number
  historyState: "idle" | "loading" | "hydrated" | "failed"
  activeConversation: ConversationSummary | null
  history: HistoryTurn[]
  activeTurn: DurableActiveTurn | null
  terminalResult: TurnResult | null
  resources: DurableResourceState
  failure: ChatFailure | null
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
  durable: DurableChatState
}

export type WorkspaceState =
  | "initial"
  | "loading"
  | "running"
  | "success"
  | "partial"
  | "empty"
  | "error"
  | "offline"
  | "cancelled"
  | "expired"
  | "conflict"
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
  | { type: "durable.history.loading"; generation: number }
  | {
      type: "durable.conversation.selected"
      generation: number
      conversation: ConversationSummary
    }
  | {
      type: "durable.history.hydrated"
      generation: number
      conversation: ConversationSummary
      turns: HistoryTurn[]
      preferences?: PreferenceRecord[]
    }
  | {
      type: "durable.history.failed"
      generation: number
      failure: ChatFailure
    }
  | {
      type: "durable.turn.started"
      generation: number
      conversationId: string
      clientTurnId: string
      message: string
    }
  | { type: "durable.turn.retried"; generation: number }
  | {
      type: "durable.turn.event"
      generation: number
      event: TurnSseEvent
    }
  | {
      type: "durable.turn.cancel.requested"
      generation: number
    }
  | {
      type: "durable.turn.reconciled" | "durable.turn.cancelled"
      generation: number
      response: TurnResponse
    }
  | {
      type: "durable.turn.transport-failed"
      generation: number
      failure: ChatFailure
    }
  | { type: "durable.action.updated"; action: ActionCard }
  | {
      type: "durable.preferences.hydrated"
      preferences: PreferenceRecord[]
    }
  | { type: "durable.reset"; generation: number }
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
    durable: createInitialDurableState(),
  }
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  if (isDurableAction(action)) {
    return reduceDurableAction(state, action)
  }

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
    if (action.configured === state.credentialConfigured) return state
    return {
      ...state,
      credentialConfigured: action.configured,
      durable: createInitialDurableState(state.durable.generation + 1),
    }
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
      durable: createInitialDurableState(
        Math.max(action.generation, state.durable.generation + 1),
      ),
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

  if (hasDurableWorkspace(state.durable)) {
    return selectDurableWorkspaceState(state)
  }

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
    state.request.phase !== "streaming" &&
    !isLiveDurableTurn(state.durable.activeTurn)
  )
}

export function selectDurableWorkspaceState(state: ChatState): WorkspaceState {
  if (!state.online) return "offline"
  if (!state.credentialConfigured) return "disabled"

  const durable = state.durable
  if (durable.historyState === "loading") return "loading"
  if (durable.failure) {
    if (isConflictCode(durable.failure.code)) return "conflict"
    if (isExpiredCode(durable.failure.code)) return "expired"
    return "error"
  }

  const active = durable.activeTurn
  if (active) {
    if (active.cancellationPending) return "running"
    if (active.status === "pending") return "loading"
    if (active.status === "running") {
      return active.streamedAnswer ? "partial" : "running"
    }
    if (active.status === "cancelled") return "cancelled"
    if (active.status === "failed" || active.status === "interrupted") {
      return "error"
    }
    if (!durable.terminalResult) {
      return active.streamedAnswer ? "partial" : "loading"
    }
    const linkedActions = actionCardsForTurn(durable, active.turnId)
    if (linkedActions.some((action) => action.status === "conflicted")) {
      return "conflict"
    }
    if (linkedActions.some((action) => action.status === "expired")) {
      return "expired"
    }
    return durable.terminalResult.answer.trim() ? "success" : "empty"
  }

  if (!durable.activeConversation) return "initial"
  if (durable.historyState === "hydrated" && durable.history.length === 0) {
    return "empty"
  }
  const latestTurn = durable.history.at(-1)
  if (latestTurn?.status === "pending") return "loading"
  if (latestTurn?.status === "running") return "running"
  if (latestTurn?.status === "cancelled") return "cancelled"
  if (latestTurn?.status === "failed" || latestTurn?.status === "interrupted") {
    return "error"
  }
  if (latestTurn?.status === "completed" && latestTurn.assistantResult) {
    const linkedActions = actionCardsForTurn(durable, latestTurn.turnId)
    if (linkedActions.some((action) => action.status === "conflicted")) {
      return "conflict"
    }
    if (linkedActions.some((action) => action.status === "expired")) {
      return "expired"
    }
    return latestTurn.assistantResult.answer.trim() ? "success" : "empty"
  }
  return "initial"
}

export function createDurableRecoveryRequest(
  state: ChatState,
): { conversationId: string; clientTurnId: string; message: string } | null {
  const active = state.durable.activeTurn
  if (
    !active ||
    active.serverSettled ||
    active.message.length === 0
  ) {
    return null
  }
  return {
    conversationId: active.conversationId,
    clientTurnId: active.clientTurnId,
    message: active.message,
  }
}

function createInitialDurableState(generation = 0): DurableChatState {
  return {
    generation,
    historyState: "idle",
    activeConversation: null,
    history: [],
    activeTurn: null,
    terminalResult: null,
    resources: emptyDurableResources(),
    failure: null,
  }
}

function reduceDurableAction(
  state: ChatState,
  action: Extract<ChatAction, { type: `durable.${string}` }>,
): ChatState {
  const durable = state.durable

  if (action.type === "durable.history.loading") {
    if (action.generation <= durable.generation) return state
    return {
      ...state,
      durable: {
        ...durable,
        generation: action.generation,
        historyState: "loading",
        failure: null,
      },
    }
  }

  if (action.type === "durable.conversation.selected") {
    if (action.generation <= durable.generation) return state
    return {
      ...state,
      durable: {
        ...createInitialDurableState(action.generation),
        activeConversation: action.conversation,
      },
    }
  }

  if (action.type === "durable.history.hydrated") {
    if (action.generation < durable.generation) return state
    const preferences = action.preferences ?? []
    const activeHistoryTurn = [...action.turns]
      .reverse()
      .find((turn) => turn.status === "pending" || turn.status === "running")
    const current = durable.activeTurn
    const sameTurn =
      activeHistoryTurn &&
      current?.conversationId === action.conversation.conversationId &&
      current.clientTurnId === activeHistoryTurn.clientTurnId
    const activeTurn = activeHistoryTurn
      ? {
          generation: action.generation,
          conversationId: action.conversation.conversationId,
          turnId: activeHistoryTurn.turnId,
          clientTurnId: activeHistoryTurn.clientTurnId,
          message: activeHistoryTurn.userMessage ?? (sameTurn ? current.message : ""),
          status: activeHistoryTurn.status,
          progress: sameTurn ? current.progress : [],
          streamedAnswer: sameTurn ? current.streamedAnswer : "",
          lastSequence: sameTurn ? current.lastSequence : 0,
          terminal: null,
          cancellationPending: sameTurn ? current.cancellationPending : false,
          serverSettled: false,
        }
      : null
    const latestResult = [...action.turns]
      .reverse()
      .find((turn) => turn.assistantResult)?.assistantResult

    return {
      ...state,
      durable: {
        generation: action.generation,
        historyState: "hydrated",
        activeConversation: action.conversation,
        history: [...action.turns],
        activeTurn,
        terminalResult: activeTurn ? null : (latestResult ?? null),
        resources: resourcesFromHistory(action.turns, preferences),
        failure: null,
      },
    }
  }

  if (action.type === "durable.history.failed") {
    if (action.generation < durable.generation) return state
    return {
      ...state,
      durable: {
        ...durable,
        generation: action.generation,
        historyState: "failed",
        failure: action.failure,
      },
    }
  }

  if (action.type === "durable.turn.started") {
    if (action.generation <= durable.generation) return state
    return {
      ...state,
      durable: {
        ...durable,
        generation: action.generation,
        activeTurn: {
          generation: action.generation,
          conversationId: action.conversationId,
          turnId: null,
          clientTurnId: action.clientTurnId,
          message: action.message,
          status: "pending",
          progress: [],
          streamedAnswer: "",
          lastSequence: 0,
          terminal: null,
          cancellationPending: false,
          serverSettled: false,
        },
        terminalResult: null,
        failure: null,
      },
    }
  }

  if (action.type === "durable.turn.retried") {
    const active = durable.activeTurn
    if (
      !active ||
      active.serverSettled ||
      action.generation <= durable.generation
    ) {
      return state
    }
    return {
      ...state,
      durable: {
        ...durable,
        generation: action.generation,
        activeTurn: {
          ...active,
          generation: action.generation,
          lastSequence: 0,
          cancellationPending: false,
        },
        failure: null,
      },
    }
  }

  if (action.type === "durable.turn.event") {
    return reduceDurableTurnEvent(state, action.generation, action.event)
  }

  if (action.type === "durable.turn.cancel.requested") {
    const active = durable.activeTurn
    if (
      !active ||
      action.generation !== active.generation ||
      active.serverSettled
    ) {
      return state
    }
    return {
      ...state,
      durable: {
        ...durable,
        activeTurn: { ...active, cancellationPending: true },
      },
    }
  }

  if (
    action.type === "durable.turn.reconciled" ||
    action.type === "durable.turn.cancelled"
  ) {
    return reconcileDurableTurn(state, action.generation, action.response)
  }

  if (action.type === "durable.turn.transport-failed") {
    const active = durable.activeTurn
    if (
      !active ||
      action.generation !== active.generation ||
      active.serverSettled
    ) {
      return state
    }
    return {
      ...state,
      durable: { ...durable, failure: action.failure },
    }
  }

  if (action.type === "durable.action.updated") {
    const existing = durable.resources.actions[action.action.actionId]
    if (existing === action.action) return state
    const artifacts = Object.fromEntries(
      Object.entries(durable.resources.artifacts).map(([artifactId, artifact]) => [
        artifactId,
        artifact.kind === "action" && artifact.actionId === action.action.actionId
          ? { ...artifact, status: action.action.status }
          : artifact,
      ]),
    ) as Record<string, TurnArtifact>
    return {
      ...state,
      durable: {
        ...durable,
        resources: {
          ...durable.resources,
          actions: {
            ...durable.resources.actions,
            [action.action.actionId]: action.action,
          },
          artifacts,
        },
      },
    }
  }

  if (action.type === "durable.preferences.hydrated") {
    return {
      ...state,
      durable: {
        ...durable,
        resources: resourcesWithPreferences(
          durable.resources,
          action.preferences,
        ),
      },
    }
  }

  if (action.type === "durable.reset") {
    if (action.generation <= durable.generation) return state
    return {
      ...state,
      durable: createInitialDurableState(action.generation),
    }
  }

  return state
}

function reduceDurableTurnEvent(
  state: ChatState,
  generation: number,
  event: TurnSseEvent,
): ChatState {
  const durable = state.durable
  const active = durable.activeTurn
  if (
    !active ||
    generation !== active.generation ||
    event.sequence <= active.lastSequence ||
    (active.turnId !== null && event.turnId !== active.turnId) ||
    active.serverSettled
  ) {
    return state
  }

  const identified =
    active.turnId === null ? { ...active, turnId: event.turnId } : active
  if (event.event === "progress") {
    return {
      ...state,
      durable: {
        ...durable,
        activeTurn: {
          ...identified,
          status: event.turnStatus,
          progress: [...identified.progress, event],
          lastSequence: event.sequence,
        },
        failure: null,
      },
    }
  }

  if (event.event === "text_delta") {
    return {
      ...state,
      durable: {
        ...durable,
        activeTurn: {
          ...identified,
          status: event.turnStatus,
          streamedAnswer: identified.streamedAnswer + event.delta,
          lastSequence: event.sequence,
        },
        failure: null,
      },
    }
  }

  const result =
    event.serverSettled && event.payload.status === "completed"
      ? event.payload.result
      : null
  return {
    ...state,
    durable: {
      ...durable,
      activeTurn: {
        ...identified,
        status: event.payload.status,
        lastSequence: event.sequence,
        terminal: event,
        cancellationPending: false,
        serverSettled: event.serverSettled,
      },
      terminalResult: result,
      resources: result
        ? resourcesWithResult(durable.resources, event.turnId, result)
        : durable.resources,
      failure:
        event.payload.status === "failed" || event.payload.status === "interrupted"
          ? toDurableFailure(event.payload.error)
          : null,
    },
  }
}

function reconcileDurableTurn(
  state: ChatState,
  generation: number,
  response: TurnResponse,
): ChatState {
  const durable = state.durable
  const active = durable.activeTurn
  if (
    !active ||
    generation !== active.generation ||
    response.conversationId !== active.conversationId ||
    response.turn.clientTurnId !== active.clientTurnId ||
    (active.turnId !== null && response.turn.turnId !== active.turnId)
  ) {
    return state
  }

  const result = response.turn.status === "completed" ? response.result : null
  return {
    ...state,
    durable: {
      ...durable,
      activeTurn: {
        ...active,
        turnId: response.turn.turnId,
        status: response.turn.status,
        terminal:
          active.terminal?.payload.status === response.turn.status
            ? active.terminal
            : null,
        cancellationPending: false,
        serverSettled: !isLiveTurnStatus(response.turn.status),
      },
      terminalResult: result,
      resources: result
        ? resourcesWithResult(durable.resources, response.turn.turnId, result)
        : durable.resources,
      failure: response.error ? toDurableFailure(response.error) : null,
    },
  }
}

function resourcesFromHistory(
  history: HistoryTurn[],
  preferences: PreferenceRecord[],
): DurableResourceState {
  let resources = emptyDurableResources()
  for (const turn of history) {
    resources = turn.assistantResult
      ? resourcesWithResult(resources, turn.turnId, turn.assistantResult)
      : {
          ...resources,
          byTurnId: {
            ...resources.byTurnId,
            [turn.turnId]: emptyDurableLinks(),
          },
        }
  }
  return resourcesWithPreferences(resources, preferences)
}

function resourcesWithResult(
  resources: DurableResourceState,
  turnId: string,
  result: TurnResult,
): DurableResourceState {
  const artifacts = { ...resources.artifacts }
  const actions = { ...resources.actions }
  for (const artifact of result.artifacts) artifacts[artifact.artifactId] = artifact
  for (const action of result.actionCards) actions[action.actionId] = action
  const existing = resources.byTurnId[turnId] ?? emptyDurableLinks()
  return {
    ...resources,
    artifacts,
    actions,
    byTurnId: {
      ...resources.byTurnId,
      [turnId]: {
        ...existing,
        artifactIds: result.artifacts.map((artifact) => artifact.artifactId),
        actionIds: result.actionCards.map((action) => action.actionId),
      },
    },
  }
}

function resourcesWithPreferences(
  resources: DurableResourceState,
  preferences: PreferenceRecord[],
): DurableResourceState {
  const nextPreferences: Record<string, PreferenceRecord> = {}
  const byTurnId = Object.fromEntries(
    Object.entries(resources.byTurnId).map(([turnId, links]) => [
      turnId,
      { ...links, preferenceIds: [] },
    ]),
  ) as Record<string, DurableTurnLinks>
  for (const preference of preferences) {
    nextPreferences[preference.preferenceId] = preference
    const existing = byTurnId[preference.sourceTurnId] ?? emptyDurableLinks()
    byTurnId[preference.sourceTurnId] = {
      ...existing,
      preferenceIds: [...existing.preferenceIds, preference.preferenceId],
    }
  }
  return { ...resources, preferences: nextPreferences, byTurnId }
}

function emptyDurableResources(): DurableResourceState {
  return { artifacts: {}, actions: {}, preferences: {}, byTurnId: {} }
}

function emptyDurableLinks(): DurableTurnLinks {
  return { artifactIds: [], actionIds: [], preferenceIds: [] }
}

function toDurableFailure(error: { code: string; message: string; retryable: boolean }): ChatFailure {
  return {
    source: "gateway",
    code: error.code,
    message: error.message,
    retryable: error.retryable,
  }
}

function hasDurableWorkspace(durable: DurableChatState): boolean {
  return (
    durable.historyState !== "idle" ||
    durable.activeConversation !== null ||
    durable.activeTurn !== null
  )
}

function isLiveDurableTurn(turn: DurableActiveTurn | null): boolean {
  return turn !== null && (!turn.serverSettled || turn.cancellationPending)
}

function isLiveTurnStatus(status: DurableTurnStatus): boolean {
  return status === "pending" || status === "running"
}

function actionCardsForTurn(
  durable: DurableChatState,
  turnId: string | null,
): ActionCard[] {
  if (!turnId) return []
  const links = durable.resources.byTurnId[turnId]
  return links?.actionIds
    .map((actionId) => durable.resources.actions[actionId])
    .filter((action): action is ActionCard => action !== undefined) ?? []
}

function isConflictCode(code: string): boolean {
  return code.includes("conflict")
}

function isExpiredCode(code: string): boolean {
  return code.includes("expired")
}

function isDurableAction(
  action: ChatAction,
): action is Extract<ChatAction, { type: `durable.${string}` }> {
  return action.type.startsWith("durable.")
}
