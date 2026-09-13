import type { ChatState, DurableActiveTurn } from "@/features/chat/chat-state"
import type {
  AgentExecution,
  GatewayChatResponse,
  GatewayStatusEvent,
  ModelCall,
  Provenance,
  TaskStatus,
} from "@/lib/contracts"
import type {
  ActionCard,
  ActionKind,
  ActionStatus,
  DialogueOutcome,
  EvidenceKind,
  EvidenceReference,
  HistoryTurn,
  PreferenceRecord,
  TurnArtifact,
  TurnResult,
  TurnSseProgressEvent,
  TurnStatus,
  UsageSummary,
} from "@/lib/v2-contracts"

export const agentLabels: Record<string, string> = {
  orchestrator: "Orchestrator",
  product_agent: "Danh mục sách",
  review_agent: "Đánh giá độc giả",
  trust_agent: "Trust signals",
  market_agent: "Snapshot stats",
}

export const phaseLabels: Record<string, string> = {
  "request.accepted": "Đã tiếp nhận",
  "routing.completed": "Đã định tuyến",
  "planning.completed": "Đã lập kế hoạch",
  "model.routing.completed": "Model định tuyến xong",
  "model.planning.completed": "Model lập kế hoạch xong",
  "agent.started": "Agent bắt đầu",
  "agent.completed": "Agent hoàn tất",
  "aggregation.completed": "Đã tổng hợp",
  "model.aggregation.completed": "Model tổng hợp xong",
  admitted: "Đã tiếp nhận",
  attached: "Đã nối lại lượt",
  claimed: "Đang điều phối",
  step_started: "Bước bắt đầu",
  step_finished: "Bước hoàn tất",
}

export const statusLabels: Record<TaskStatus, string> = {
  pending: "Chờ",
  running: "Đang chạy",
  success: "Hoàn tất",
  partial_success: "Một phần",
  failed: "Thất bại",
}

export const turnStatusLabels: Record<TurnStatus, string> = {
  pending: "Đang chờ",
  running: "Đang chạy",
  completed: "Hoàn tất",
  failed: "Thất bại",
  cancelled: "Đã dừng",
  interrupted: "Bị gián đoạn",
}

export const dialogueOutcomeLabels: Record<DialogueOutcome, string> = {
  answered: "Đã trả lời",
  needs_clarification: "Cần làm rõ",
  awaiting_confirmation: "Chờ xác nhận",
  abstained: "Không đủ cơ sở trả lời",
}

export const actionStatusLabels: Record<ActionStatus, string> = {
  proposed: "Chờ quyết định",
  confirmed: "Đã xác nhận",
  rejected: "Đã từ chối",
  executed: "Đã thực hiện",
  expired: "Đã hết hạn",
  conflicted: "Có xung đột",
  failed: "Thất bại",
}

export const actionKindLabels: Record<ActionKind, string> = {
  cart_change: "Thay đổi giỏ hàng",
  checkout: "Thanh toán",
  merchant_price_change: "Thay đổi giá",
  merchant_inventory_change: "Thay đổi tồn kho",
}

export const evidenceKindLabels: Record<EvidenceKind, string> = {
  catalog: "Danh mục",
  review: "Đánh giá",
  trust: "Tín hiệu tin cậy",
  market: "Thị trường",
  knowledge: "Tri thức",
  sandbox: "Sandbox",
}

export const artifactKindLabels: Record<TurnArtifact["kind"], string> = {
  product_comparison: "So sánh sản phẩm",
  cart: "Giỏ hàng",
  order: "Đơn hàng",
  action: "Hành động",
}

export type PresentationTaskStatus =
  | TaskStatus
  | "cancelled"
  | "not_run"

export type RosterIcon =
  | "orchestrator"
  | "catalog"
  | "review"
  | "trust"
  | "market"
  | "generic"

export interface RosterItemView {
  id: string
  name: string
  role: string
  index: string
  status: PresentationTaskStatus
  icon: RosterIcon
}

export interface RouteStepView {
  stepId: string
  capability: string
  service: string | null
  label: string
  dependsOn: string[]
  status: TaskStatus
  durationMs: number | null
  reused: boolean
  errorMessage: string | null
}

export interface EvidenceView {
  evidenceId: string
  sourceId: string
  sourceVersionId: string | null
  displayLabel: string
  kind: EvidenceKind | "legacy"
  kindLabel: string
  title: string
  url: string | null
  observedAt: string
  fields: string[]
  sampleData: boolean | null
}

export interface CitationTargetView {
  citationId: string
  claimId: string
  displayLabel: string
  evidenceId: string
  evidenceTitle: string
}

export interface ClaimView {
  claimId: string
  text: string
  citationIds: string[]
}

export interface ActionChangeView {
  resourceType: string
  resourceId: string
  field: "quantity" | "price_vnd" | "status"
  beforeInteger: number | null
  afterInteger: number | null
  beforeText: string | null
  afterText: string | null
}

export interface ActionCardView {
  actionId: string
  proposalId: string
  proposalVersion: number
  kind: ActionKind
  kindLabel: string
  status: ActionStatus
  statusLabel: string
  title: string
  requiredPermission: string
  confirmationRequired: boolean
  resourceType: string
  resourceId: string
  expectedResourceVersion: number
  dataVersionIds: string[]
  changes: ActionChangeView[]
  expiresAt: string
}

interface ArtifactBaseView {
  artifactId: string
  resourceId: string
  resourceVersion: number
  title: string
  kindLabel: string
}

export interface ArtifactLineItemView {
  productId: number
  title: string
  quantity: number
  unitPriceVnd: number
  lineTotalVnd: number
}

export type ArtifactView =
  | (ArtifactBaseView & {
      kind: "product_comparison"
      status: "ready" | "partial"
      products: Array<{
        productId: number
        title: string
        author: string | null
        priceVnd: number | null
        rating: number | null
        catalogVersionId: string
      }>
      dataVersionIds: string[]
    })
  | (ArtifactBaseView & {
      kind: "cart"
      status: "active" | "checked_out" | "abandoned"
      items: ArtifactLineItemView[]
      totalPriceVnd: number
      dataVersionIds: string[]
    })
  | (ArtifactBaseView & {
      kind: "order"
      status: "confirmed"
      cartId: string
      cartVersion: number
      items: ArtifactLineItemView[]
      totalPriceVnd: number
      dataVersionIds: string[]
      createdAt: string
    })
  | (ArtifactBaseView & {
      kind: "action"
      actionId: string
      proposalId: string
      proposalVersion: number
      actionKind: ActionKind
      actionStatus: ActionStatus
    })

export interface ConversationAssistantView {
  text: string
  claims: ClaimView[]
  citations: CitationTargetView[]
  evidence: EvidenceView[]
  artifacts: ArtifactView[]
  actions: ActionCardView[]
  warnings: string[]
}

export interface ConversationTurnView {
  protocol: "v1" | "v2"
  key: string
  turnId: string | null
  clientTurnId: string | null
  userMessage: string | null
  assistant: ConversationAssistantView | null
  status: TurnStatus
  statusLabel: string
  outcome: DialogueOutcome | null
  outcomeLabel: string | null
  createdAt: string | null
  completedAt: string | null
  isActive: boolean
  isStreaming: boolean
  error: { code: string; message: string; retryable: boolean } | null
}

export interface ModelCallView {
  callId: string
  stage: string
  agentLabel: string
  provider: string
  model: string
  status: string
  durationMs: number
  totalTokens: number
  attempts: number
  fallbackUsed: boolean
  fallbackReason: string | null
  errorCode: string | null
}

export interface UsageView {
  totalTokens: number
  inputTokens: number
  outputTokens: number
  generationCalls: number
  providerAttempts: number
  knowledgeRetrievals: number
  fallbackUsed: boolean
  estimatedCostUsd: string
}

export interface IdentifierView {
  label:
    | "request"
    | "trace"
    | "session"
    | "conversation"
    | "turn"
    | "client turn"
  value: string | null
}

export interface DossierView {
  protocol: "v1" | "v2"
  status: PresentationTaskStatus
  statusLabel: string
  route: RouteStepView[]
  modelCalls: ModelCallView[]
  usage: UsageView | null
  durationMs: number | null
  evidence: EvidenceView[]
  identifiers: IdentifierView[]
  selectedTurnId: string | null
}

export function friendlyAgent(agentId: string | null): string {
  if (!agentId) return "Toàn hệ thống"
  return agentLabels[agentId] ?? humanizeIdentifier(agentId)
}

export function friendlyPhase(phase: string): string {
  return phaseLabels[phase] ?? humanizeIdentifier(phase)
}

export function friendlyCapability(capability: string): string {
  const labels: Record<string, string> = {
    "catalog.search": "Tìm danh mục",
    "catalog.read": "Đọc danh mục",
    "reviews.search": "Tìm đánh giá",
    "reviews.summarize": "Tóm tắt đánh giá",
    "trust.evaluate": "Đánh giá tin cậy",
    "market.analyze": "Phân tích thị trường",
    "knowledge.retrieve": "Truy xuất tri thức",
  }
  return labels[capability] ?? humanizeIdentifier(capability)
}

export function friendlyService(service: string | null): string {
  if (!service) return "Dịch vụ theo tiến trình"
  return humanizeIdentifier(service)
}

export function formatDuration(value: number): string {
  if (value < 1_000) return String(Math.round(value)) + " ms"
  return (value / 1_000).toFixed(2) + " s"
}

export function formatVnd(value: number): string {
  return new Intl.NumberFormat("vi-VN", {
    style: "currency",
    currency: "VND",
    maximumFractionDigits: 0,
  }).format(value)
}

export function formatEvidenceLabel(evidence: EvidenceView): string {
  return evidence.displayLabel + " · " + evidence.title
}

export function formatArtifactLabel(artifact: ArtifactView): string {
  return artifact.kindLabel + " · " + artifact.title
}

export function shortenIdentifier(value: string, maxLength = 24): string {
  if (value.length <= maxLength) return value
  const visible = Math.max(6, Math.floor((maxLength - 1) / 2))
  return value.slice(0, visible) + "…" + value.slice(-visible)
}

export type ProvenanceAnchorScope = "desktop" | "mobile"

export function provenanceAnchorId(
  scope: ProvenanceAnchorScope,
  evidenceId: string,
): string {
  return "provenance-" + scope + "-" + encodeURIComponent(evidenceId)
}

export function projectEvidence(result: TurnResult): EvidenceView[] {
  return result.evidence.map(projectEvidenceReference)
}

export function projectCitationTargets(
  result: TurnResult,
): CitationTargetView[] {
  const evidence = new Map(
    result.evidence.map((item) => [item.evidenceId, item]),
  )
  return result.citations.flatMap((citation) => {
    const item = evidence.get(citation.evidenceId)
    if (!item || item.displayLabel !== citation.displayLabel) return []
    return [
      {
        citationId: citation.citationId,
        claimId: citation.claimId,
        displayLabel: citation.displayLabel,
        evidenceId: item.evidenceId,
        evidenceTitle: item.title,
      },
    ]
  })
}

export function evidenceIdForDisplayLabel(
  result: TurnResult,
  displayLabel: string,
): string | null {
  const matches = projectCitationTargets(result).filter(
    (citation) => citation.displayLabel === displayLabel,
  )
  if (matches.length !== 1) return null
  return matches[0].evidenceId
}

export function projectActionCard(action: ActionCard): ActionCardView {
  return {
    actionId: action.actionId,
    proposalId: action.proposalId,
    proposalVersion: action.proposalVersion,
    kind: action.kind,
    kindLabel: actionKindLabels[action.kind],
    status: action.status,
    statusLabel: actionStatusLabels[action.status],
    title: action.title,
    requiredPermission: action.requiredPermission,
    confirmationRequired: action.confirmationRequired,
    resourceType: action.target.resourceType,
    resourceId: action.target.resourceId,
    expectedResourceVersion: action.target.expectedResourceVersion,
    dataVersionIds: [...action.target.dataVersionIds],
    changes: action.changes.map((change) => ({
      resourceType: change.resourceType,
      resourceId: change.resourceId,
      field: change.field,
      beforeInteger: change.beforeInteger,
      afterInteger: change.afterInteger,
      beforeText: change.beforeText,
      afterText: change.afterText,
    })),
    expiresAt: action.expiresAt,
  }
}

export function projectArtifact(artifact: TurnArtifact): ArtifactView {
  const base = {
    artifactId: artifact.artifactId,
    resourceId: artifact.resourceId,
    resourceVersion: artifact.resourceVersion,
    title: artifact.title,
    kindLabel: artifactKindLabels[artifact.kind],
  }
  if (artifact.kind === "product_comparison") {
    return {
      ...base,
      kind: artifact.kind,
      status: artifact.status,
      products: artifact.products.map((product) => ({
        productId: product.productId,
        title: product.title,
        author: product.author,
        priceVnd: product.priceVnd,
        rating: product.rating,
        catalogVersionId: product.catalogVersionId,
      })),
      dataVersionIds: [...artifact.dataVersionIds],
    }
  }
  if (artifact.kind === "action") {
    return {
      ...base,
      kind: artifact.kind,
      actionId: artifact.actionId,
      proposalId: artifact.proposalId,
      proposalVersion: artifact.proposalVersion,
      actionKind: artifact.actionKind,
      actionStatus: artifact.status,
    }
  }
  const items = artifact.items.map(projectArtifactLineItem)
  if (artifact.kind === "cart") {
    return {
      ...base,
      kind: artifact.kind,
      status: artifact.status,
      items,
      totalPriceVnd: artifact.totalPriceVnd,
      dataVersionIds: [...artifact.dataVersionIds],
    }
  }
  return {
    ...base,
    kind: artifact.kind,
    status: artifact.status,
    cartId: artifact.cartId,
    cartVersion: artifact.cartVersion,
    items,
    totalPriceVnd: artifact.totalPriceVnd,
    dataVersionIds: [...artifact.dataVersionIds],
    createdAt: artifact.createdAt,
  }
}

export function selectConversationTurnViews(
  state: ChatState,
): ConversationTurnView[] {
  if (!hasDurablePresentation(state)) return projectLegacyTurns(state)

  const views = state.durable.history.map((turn) =>
    projectHistoryTurn(state, turn),
  )
  const active = state.durable.activeTurn
  if (!active) return views

  const matchingIndex = views.findIndex(
    (view) =>
      (active.turnId !== null && view.turnId === active.turnId) ||
      view.clientTurnId === active.clientTurnId,
  )
  if (matchingIndex >= 0) {
    const authoritative = views[matchingIndex]
    views[matchingIndex] = {
      ...authoritative,
      isActive: true,
      isStreaming: isLiveTurnStatus(authoritative.status),
      assistant:
        authoritative.assistant ?? projectActiveAssistant(state, active),
    }
    return views
  }

  views.push(projectActiveTurn(state, active))
  return views
}

export function selectTurnView(
  state: ChatState,
  selectedTurnId?: string,
): ConversationTurnView | null {
  const turns = selectConversationTurnViews(state)
  if (selectedTurnId) {
    const selected = turns.find(
      (turn) =>
        turn.turnId === selectedTurnId ||
        turn.clientTurnId === selectedTurnId,
    )
    if (selected) return selected
  }
  return turns.findLast((turn) => turn.isActive) ?? turns.at(-1) ?? null
}

export function createLegacyRosterItems(
  executions: AgentExecution[],
  statuses: GatewayStatusEvent[],
  requestPhase: "idle" | "streaming" | "completed" | "failed" | "cancelled",
): RosterItemView[] {
  const definitions: Array<Omit<RosterItemView, "status">> = [
    {
      id: "orchestrator",
      name: "Orchestrator",
      role: "Định tuyến · DAG · tổng hợp",
      index: "00",
      icon: "orchestrator",
    },
    {
      id: "product_agent",
      name: "Danh mục sách",
      role: "Tựa sách · giá · thuộc tính",
      index: "01",
      icon: "catalog",
    },
    {
      id: "review_agent",
      name: "Đánh giá độc giả",
      role: "Nhận xét · khía cạnh · tóm tắt",
      index: "02",
      icon: "review",
    },
    {
      id: "trust_agent",
      name: "Trust signals",
      role: "Phản hồi tiêu cực · tín hiệu rủi ro",
      index: "03",
      icon: "trust",
    },
    {
      id: "market_agent",
      name: "Snapshot stats",
      role: "Thể loại · giá · thống kê cắt ngang",
      index: "04",
      icon: "market",
    },
  ]
  return definitions.map((definition) => ({
    ...definition,
    status: resolveLegacyAgentStatus(
      definition.id,
      executions,
      statuses,
      requestPhase,
    ),
  }))
}

export function selectRosterItems(
  state: ChatState,
  selectedTurnId?: string,
): RosterItemView[] {
  if (!hasDurablePresentation(state)) {
    const result =
      state.request.phase === "completed" ? state.request.result : null
    return createLegacyRosterItems(
      result?.executions ?? [],
      state.request.phase === "streaming" ? state.request.statuses : [],
      state.request.phase,
    )
  }

  const context = selectDurableContext(state, selectedTurnId)
  if (!context) return []
  const route = projectDurableRoute(context.result, context.progress)
  return [
    {
      id: "orchestrator",
      name: "Orchestrator",
      role: "Định tuyến · DAG · tổng hợp",
      index: "00",
      status: turnStatusToPresentationStatus(context.status),
      icon: "orchestrator",
    },
    ...route.map((step, index) => ({
      id: step.stepId,
      name: friendlyCapability(step.capability),
      role:
        friendlyService(step.service) +
        " · " +
        friendlyCapability(step.capability),
      index: String(index + 1).padStart(2, "0"),
      status: step.status,
      icon: iconForCapability(step.capability),
    })),
  ]
}

export function selectDossierView(
  state: ChatState,
  selectedTurnId?: string,
): DossierView {
  if (!hasDurablePresentation(state)) return projectLegacyDossier(state)

  const context = selectDurableContext(state, selectedTurnId)
  if (!context) {
    return {
      protocol: "v2",
      status: "not_run",
      statusLabel: "Chưa chạy",
      route: [],
      modelCalls: [],
      usage: null,
      durationMs: null,
      evidence: [],
      identifiers: [
        {
          label: "conversation",
          value: state.durable.activeConversation?.conversationId ?? null,
        },
      ],
      selectedTurnId: null,
    }
  }

  const envelope = context.active?.terminal ?? context.progress.at(-1) ?? null
  const status = turnStatusToPresentationStatus(context.status)
  return {
    protocol: "v2",
    status,
    statusLabel: presentationStatusLabel(status),
    route: projectDurableRoute(context.result, context.progress),
    modelCalls: [],
    usage: context.active?.terminal
      ? projectUsage(context.active.terminal.payload.usage)
      : null,
    durationMs: null,
    evidence: context.result ? projectEvidence(context.result) : [],
    identifiers: [
      { label: "request", value: envelope?.requestId ?? null },
      { label: "trace", value: envelope?.traceId ?? null },
      {
        label: "conversation",
        value:
          state.durable.activeConversation?.conversationId ??
          context.active?.conversationId ??
          null,
      },
      { label: "turn", value: context.turnId },
      { label: "client turn", value: context.clientTurnId },
    ],
    selectedTurnId: context.turnId,
  }
}

function projectHistoryTurn(
  state: ChatState,
  turn: HistoryTurn,
): ConversationTurnView {
  const result = turn.assistantResult
  return {
    protocol: "v2",
    key: "v2:" + turn.turnId,
    turnId: turn.turnId,
    clientTurnId: turn.clientTurnId,
    userMessage: turn.userMessage,
    assistant: result ? projectAssistant(state, turn.turnId, result) : null,
    status: turn.status,
    statusLabel: turnStatusLabels[turn.status],
    outcome: turn.outcome,
    outcomeLabel: turn.outcome ? dialogueOutcomeLabels[turn.outcome] : null,
    createdAt: turn.createdAt,
    completedAt: turn.completedAt,
    isActive: false,
    isStreaming: isLiveTurnStatus(turn.status),
    error: turn.error
      ? {
          code: turn.error.code,
          message: turn.error.message,
          retryable: turn.error.retryable,
        }
      : null,
  }
}

function projectActiveTurn(
  state: ChatState,
  active: DurableActiveTurn,
): ConversationTurnView {
  const terminal = active.terminal?.payload
  const outcome =
    terminal?.status === "completed" ? terminal.result.outcome : null
  const error =
    terminal &&
    (terminal.status === "failed" || terminal.status === "interrupted")
      ? terminal.error
      : null
  return {
    protocol: "v2",
    key: "v2-active:" + active.clientTurnId,
    turnId: active.turnId,
    clientTurnId: active.clientTurnId,
    userMessage: active.message,
    assistant: projectActiveAssistant(state, active),
    status: active.status,
    statusLabel: turnStatusLabels[active.status],
    outcome,
    outcomeLabel: outcome ? dialogueOutcomeLabels[outcome] : null,
    createdAt: null,
    completedAt: null,
    isActive: true,
    isStreaming: isLiveTurnStatus(active.status),
    error: error
      ? { code: error.code, message: error.message, retryable: error.retryable }
      : null,
  }
}

function projectActiveAssistant(
  state: ChatState,
  active: DurableActiveTurn,
): ConversationAssistantView | null {
  const result =
    active.terminal?.payload.status === "completed"
      ? active.terminal.payload.result
      : state.durable.terminalResult
  if (result) return projectAssistant(state, active.turnId, result)
  if (!active.streamedAnswer) return null
  return {
    text: active.streamedAnswer,
    claims: [],
    citations: [],
    evidence: [],
    artifacts: [],
    actions: [],
    warnings: [],
  }
}

function projectAssistant(
  state: ChatState,
  turnId: string | null,
  result: TurnResult,
): ConversationAssistantView {
  const links = turnId ? state.durable.resources.byTurnId[turnId] : undefined
  const linkedArtifacts = links?.artifactIds
    .map((id) => state.durable.resources.artifacts[id])
    .filter((artifact): artifact is TurnArtifact => artifact !== undefined)
  const linkedActions = links?.actionIds
    .map((id) => state.durable.resources.actions[id])
    .filter((action): action is ActionCard => action !== undefined)
  return {
    text: result.answer,
    claims: result.claims.map((claim) => ({
      claimId: claim.claimId,
      text: claim.text,
      citationIds: [...claim.citationIds],
    })),
    citations: projectCitationTargets(result),
    evidence: projectEvidence(result),
    artifacts: (linkedArtifacts ?? result.artifacts).map(projectArtifact),
    actions: (linkedActions ?? result.actionCards).map(projectActionCard),
    warnings: [...result.warnings],
  }
}

function projectLegacyTurns(state: ChatState): ConversationTurnView[] {
  const turns: ConversationTurnView[] = []
  for (const message of state.messages) {
    if (message.role === "user" || turns.length === 0) {
      turns.push({
        protocol: "v1",
        key: "v1:" + message.id,
        turnId: null,
        clientTurnId: null,
        userMessage: message.role === "user" ? message.text : null,
        assistant:
          message.role === "assistant" ? emptyAssistant(message.text) : null,
        status: "completed",
        statusLabel: turnStatusLabels.completed,
        outcome: null,
        outcomeLabel: null,
        createdAt: null,
        completedAt: null,
        isActive: false,
        isStreaming: false,
        error: null,
      })
      continue
    }
    const current = turns.at(-1)!
    current.assistant = emptyAssistant(message.text)
  }

  const current = turns.at(-1)
  if (!current) return turns
  if (state.request.phase === "streaming") {
    current.status = "running"
    current.statusLabel = turnStatusLabels.running
    current.isActive = true
    current.isStreaming = true
    if (state.request.answer) {
      current.assistant = emptyAssistant(state.request.answer)
    }
  } else if (state.request.phase === "failed") {
    current.status = "failed"
    current.statusLabel = turnStatusLabels.failed
    current.error = {
      code: state.request.failure.code,
      message: state.request.failure.message,
      retryable: state.request.failure.retryable,
    }
  } else if (state.request.phase === "cancelled") {
    current.status = "cancelled"
    current.statusLabel = turnStatusLabels.cancelled
  } else if (state.request.phase === "completed") {
    current.assistant = projectLegacyAssistant(state.request.result)
  }
  return turns
}

function emptyAssistant(text: string): ConversationAssistantView {
  return {
    text,
    claims: [],
    citations: [],
    evidence: [],
    artifacts: [],
    actions: [],
    warnings: [],
  }
}

function projectLegacyAssistant(
  result: GatewayChatResponse,
): ConversationAssistantView {
  return {
    ...emptyAssistant(result.answer),
    evidence: result.provenance.map(projectLegacyProvenance),
    citations: result.provenance.map((source, index) => ({
      citationId: "legacy:" + source.source_id + ":" + String(index),
      claimId: "legacy:" + source.source_id + ":" + String(index),
      displayLabel: source.source_id,
      evidenceId: source.source_id,
      evidenceTitle: source.source_id,
    })),
    warnings: [...result.warnings],
  }
}

function projectEvidenceReference(
  evidence: EvidenceReference,
): EvidenceView {
  return {
    evidenceId: evidence.evidenceId,
    sourceId: evidence.sourceId,
    sourceVersionId: evidence.sourceVersionId,
    displayLabel: evidence.displayLabel,
    kind: evidence.kind,
    kindLabel: evidenceKindLabels[evidence.kind],
    title: evidence.title,
    url: evidence.url,
    observedAt: evidence.observedAt,
    fields: [],
    sampleData: null,
  }
}

function projectLegacyProvenance(source: Provenance): EvidenceView {
  return {
    evidenceId: source.source_id,
    sourceId: source.source_id,
    sourceVersionId: null,
    displayLabel: source.source_id,
    kind: "legacy",
    kindLabel: source.source_type,
    title: source.source_id,
    url: null,
    observedAt: source.observed_at,
    fields: [...source.fields],
    sampleData: source.sample_data,
  }
}

function projectArtifactLineItem(item: {
  productId: number
  title: string
  quantity: number
  unitPriceVnd: number
  lineTotalVnd: number
}): ArtifactLineItemView {
  return {
    productId: item.productId,
    title: item.title,
    quantity: item.quantity,
    unitPriceVnd: item.unitPriceVnd,
    lineTotalVnd: item.lineTotalVnd,
  }
}

interface DurableSelectionContext {
  turnId: string | null
  clientTurnId: string
  status: TurnStatus
  result: TurnResult | null
  progress: TurnSseProgressEvent[]
  active: DurableActiveTurn | null
}

function selectDurableContext(
  state: ChatState,
  selectedTurnId?: string,
): DurableSelectionContext | null {
  const active = state.durable.activeTurn
  const activeSelected =
    active &&
    (!selectedTurnId ||
      active.turnId === selectedTurnId ||
      active.clientTurnId === selectedTurnId)
  if (activeSelected) {
    return {
      turnId: active.turnId,
      clientTurnId: active.clientTurnId,
      status: active.status,
      result:
        active.terminal?.payload.status === "completed"
          ? active.terminal.payload.result
          : state.durable.terminalResult,
      progress: active.progress,
      active,
    }
  }

  const history = selectedTurnId
    ? state.durable.history.find(
        (turn) =>
          turn.turnId === selectedTurnId ||
          turn.clientTurnId === selectedTurnId,
      )
    : state.durable.history.at(-1)
  if (!history) return null
  const matchingActive =
    active &&
    (active.turnId === history.turnId ||
      active.clientTurnId === history.clientTurnId)
      ? active
      : null
  return {
    turnId: history.turnId,
    clientTurnId: history.clientTurnId,
    status: history.status,
    result: history.assistantResult,
    progress: matchingActive?.progress ?? [],
    active: matchingActive,
  }
}

function projectDurableRoute(
  result: TurnResult | null,
  progress: TurnSseProgressEvent[],
): RouteStepView[] {
  const latestProgress = new Map<string, TurnSseProgressEvent>()
  for (const event of progress) {
    if (event.stepId) latestProgress.set(event.stepId, event)
  }
  const executions = new Map(
    (result?.executions ?? []).map((execution) => [
      execution.stepId,
      execution,
    ]),
  )
  const finalRevision = result?.plan?.revisions.at(-1)
  const route: RouteStepView[] = (finalRevision?.steps ?? []).map((step) => {
    const execution = executions.get(step.stepId)
    const event = latestProgress.get(step.stepId)
    return {
      stepId: step.stepId,
      capability: step.capability,
      service: step.service,
      label: friendlyCapability(step.capability),
      dependsOn: [...step.dependsOn],
      status: execution?.status ?? progressStatus(event),
      durationMs: execution?.durationMs ?? null,
      reused:
        execution?.reused ??
        finalRevision?.reusedStepIds.includes(step.stepId) ??
        false,
      errorMessage: execution?.error?.message ?? null,
    }
  })

  const known = new Set(route.map((step) => step.stepId))
  for (const execution of result?.executions ?? []) {
    if (known.has(execution.stepId)) continue
    route.push({
      stepId: execution.stepId,
      capability: execution.capability,
      service: execution.service,
      label: friendlyCapability(execution.capability),
      dependsOn: [],
      status: execution.status,
      durationMs: execution.durationMs,
      reused: execution.reused,
      errorMessage: execution.error?.message ?? null,
    })
    known.add(execution.stepId)
  }
  for (const [stepId, event] of latestProgress) {
    if (known.has(stepId) || !event.capability) continue
    route.push({
      stepId,
      capability: event.capability,
      service: null,
      label: friendlyCapability(event.capability),
      dependsOn: [],
      status: progressStatus(event),
      durationMs: null,
      reused: event.reused,
      errorMessage: null,
    })
  }
  return route
}

function projectLegacyDossier(state: ChatState): DossierView {
  const result =
    state.request.phase === "completed" ? state.request.result : null
  const status = legacyRequestStatus(state)
  return {
    protocol: "v1",
    status,
    statusLabel: presentationStatusLabel(status),
    route: (result?.executions ?? []).map(projectLegacyExecution),
    modelCalls: (result?.model_calls ?? []).map(projectLegacyModelCall),
    usage: null,
    durationMs: result?.duration_ms ?? null,
    evidence: (result?.provenance ?? []).map(projectLegacyProvenance),
    identifiers: [
      { label: "request", value: result?.request_id ?? null },
      { label: "trace", value: result?.trace_id ?? null },
      { label: "session", value: result?.session_id ?? state.sessionId },
    ],
    selectedTurnId: null,
  }
}

function projectLegacyExecution(
  execution: AgentExecution,
): RouteStepView {
  return {
    stepId: execution.step_id,
    capability: execution.action,
    service: execution.agent_id,
    label: friendlyAgent(execution.agent_id),
    dependsOn: [...execution.depends_on],
    status: execution.status,
    durationMs: execution.duration_ms,
    reused: false,
    errorMessage: execution.error_codes.at(0) ?? null,
  }
}

function projectLegacyModelCall(call: ModelCall): ModelCallView {
  return {
    callId: call.call_id,
    stage: call.stage,
    agentLabel: friendlyAgent(call.agent_id),
    provider: call.provider,
    model: call.model,
    status: call.status,
    durationMs: call.duration_ms,
    totalTokens: call.total_tokens,
    attempts: call.attempts,
    fallbackUsed: call.fallback_used,
    fallbackReason: call.fallback_reason,
    errorCode: call.error_code,
  }
}

function projectUsage(usage: UsageSummary): UsageView {
  return {
    totalTokens: usage.totalTokens,
    inputTokens: usage.inputTokens,
    outputTokens: usage.outputTokens,
    generationCalls: usage.generationCalls,
    providerAttempts: usage.providerAttempts,
    knowledgeRetrievals: usage.knowledgeRetrievals,
    fallbackUsed: usage.fallbackUsed,
    estimatedCostUsd: usage.estimatedCostUsd,
  }
}

function resolveLegacyAgentStatus(
  agentId: string,
  executions: AgentExecution[],
  statuses: GatewayStatusEvent[],
  requestPhase: "idle" | "streaming" | "completed" | "failed" | "cancelled",
): PresentationTaskStatus {
  if (agentId === "orchestrator") {
    if (requestPhase === "streaming") return "running"
    if (requestPhase === "completed") return "success"
    if (requestPhase === "failed") return "failed"
    if (requestPhase === "cancelled") return "cancelled"
    return "pending"
  }
  const execution = executions.find((item) => item.agent_id === agentId)
  if (execution) return execution.status
  if (requestPhase === "cancelled" || requestPhase === "failed") {
    return "not_run"
  }
  return (
    statuses.findLast((item) => item.agent_id === agentId)?.status ?? "pending"
  )
}

function progressStatus(event?: TurnSseProgressEvent): TaskStatus {
  if (!event) return "pending"
  if (event.phase === "step_finished" && event.stepStatus) {
    return event.stepStatus
  }
  return "running"
}

function turnStatusToPresentationStatus(
  status: TurnStatus,
): PresentationTaskStatus {
  if (status === "completed") return "success"
  if (status === "cancelled") return "cancelled"
  if (status === "failed" || status === "interrupted") return "failed"
  return status
}

function legacyRequestStatus(state: ChatState): PresentationTaskStatus {
  if (state.request.phase === "streaming") return "running"
  if (state.request.phase === "failed") return "failed"
  if (state.request.phase === "cancelled") return "cancelled"
  if (state.request.phase === "completed") return state.request.result.status
  return "not_run"
}

function presentationStatusLabel(status: PresentationTaskStatus): string {
  if (status === "cancelled") return "Đã dừng"
  if (status === "not_run") return "Chưa chạy"
  return statusLabels[status]
}

function iconForCapability(capability: string): RosterIcon {
  if (capability.startsWith("catalog.")) return "catalog"
  if (capability.startsWith("reviews.")) return "review"
  if (capability.startsWith("trust.")) return "trust"
  if (capability.startsWith("market.")) return "market"
  return "generic"
}

function isLiveTurnStatus(status: TurnStatus): boolean {
  return status === "pending" || status === "running"
}

function hasDurablePresentation(state: ChatState): boolean {
  return (
    state.durable.historyState !== "idle" ||
    state.durable.activeConversation !== null ||
    state.durable.history.length > 0 ||
    state.durable.activeTurn !== null
  )
}

function humanizeIdentifier(value: string): string {
  return (
    value
      .split(/[._-]+/)
      .filter(Boolean)
      .map(
        (part) =>
          part.charAt(0).toUpperCase() +
          part.slice(1),
      )
      .join(" ") || value
  )
}

export function formatPreference(preference: PreferenceRecord): string {
  const value = preference.preference
  if (value.kind === "max_budget_vnd") return formatVnd(value.value)
  return value.value
}
