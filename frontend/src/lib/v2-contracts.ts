import { z } from "zod/mini"

export const MAX_V2_MESSAGE_LENGTH = 2_000
export const MAX_V2_PLAN_REVISIONS = 1
export const MAX_V2_ADDED_READ_STEPS = 2
export const MAX_V2_EXPERT_STEPS = 8
export const MAX_V2_CANDIDATES = 5
export const MAX_V2_KNOWLEDGE_RETRIEVALS = 2
export const MAX_V2_DRAFT_REPAIRS = 1
export const MAX_V2_ARTIFACTS = 16
export const MAX_V2_ARTIFACT_ITEMS = 100
export const MAX_V2_ARTIFACT_DATA_VERSIONS = 16
export const MAX_V2_SSE_EVENTS = 10_000
export const MAX_V2_TEXT_DELTA_LENGTH = 4_000

const identifierPattern = /^[a-z][a-z0-9_-]{2,127}$/
const clientTurnIdPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
const actionPattern = /^[a-z][a-z0-9_.-]{1,127}$/
const displayCitationPattern = /^\[C[1-9][0-9]*\]$/
const decimalPattern = /^(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/

const nonEmptyString = (maximum?: number) =>
  z.string().check(
    z.minLength(1),
    ...(maximum === undefined ? [] : [z.maxLength(maximum)]),
  )
const nonnegativeNumberSchema = z.number().check(z.nonnegative())
const nonnegativeIntegerSchema = z.int().check(z.nonnegative())
const positiveIntegerSchema = z.int().check(z.positive())
const identifierSchema = z.string().check(z.regex(identifierPattern))
const actionNameSchema = z.string().check(z.regex(actionPattern))
const resourceVersionSchema = z.int().check(z.minimum(1))
const priceVndSchema = z.int().check(z.minimum(1), z.maximum(10_000_000_000))
const artifactAmountVndSchema = z.int().check(
  z.nonnegative(),
  z.maximum(10_000_000_000_000_000),
)
const awareDatetimeSchema = z.iso.datetime({ offset: true })
const httpUrlSchema = z.url().check(
  z.refine((value) => {
    const protocol = new URL(value).protocol
    return protocol === "http:" || protocol === "https:"
  }),
)
const decimalStringSchema = z.string().check(z.regex(decimalPattern))

function unique<T>(values: readonly T[]): boolean {
  return new Set(values).size === values.length
}

function sameJson(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right)
}

function dateDoesNotPrecede(later: string, earlier: string): boolean {
  return Date.parse(later) >= Date.parse(earlier)
}

interface DecimalParts {
  coefficient: bigint
  scale: number
}

function parseDecimal(value: string): DecimalParts | null {
  const match = /^(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(value)
  if (!match) return null
  const exponent = Number(match[3] ?? "0")
  const fraction = match[2] ?? ""
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 10_000) {
    return null
  }
  return {
    coefficient: BigInt(`${match[1]}${fraction}`),
    scale: fraction.length - exponent,
  }
}

function decimalEqualsSum(total: string, parts: readonly string[]): boolean {
  const parsed = [total, ...parts].map(parseDecimal)
  if (parsed.some((part) => part === null)) return false
  const values = parsed as DecimalParts[]
  const scale = Math.max(...values.map((value) => value.scale))
  const align = (value: DecimalParts) =>
    value.coefficient * 10n ** BigInt(scale - value.scale)
  return align(values[0]) === values.slice(1).reduce(
    (sum, value) => sum + align(value),
    0n,
  )
}

export const conversationModeSchema = z.enum(["shopper", "merchant"])
export const dialogueOutcomeSchema = z.enum([
  "answered",
  "needs_clarification",
  "awaiting_confirmation",
  "abstained",
])
export const turnStatusSchema = z.enum([
  "pending",
  "running",
  "completed",
  "failed",
  "cancelled",
  "interrupted",
])
export const taskStatusSchema = z.enum([
  "pending",
  "running",
  "success",
  "partial_success",
  "failed",
])
export const evidenceKindSchema = z.enum([
  "catalog",
  "review",
  "trust",
  "market",
  "knowledge",
  "sandbox",
])
export const actionKindSchema = z.enum([
  "cart_change",
  "checkout",
  "merchant_price_change",
  "merchant_inventory_change",
])
export const actionStatusSchema = z.enum([
  "proposed",
  "confirmed",
  "rejected",
  "executed",
  "expired",
  "conflicted",
  "failed",
])
export const artifactKindSchema = z.enum([
  "product_comparison",
  "cart",
  "order",
  "action",
])
export const turnSseEventKindSchema = z.enum([
  "progress",
  "text_delta",
  "terminal",
])
export const turnSseProgressPhaseSchema = z.enum([
  "admitted",
  "attached",
  "claimed",
  "step_started",
  "step_finished",
])
export const preferenceKindSchema = z.enum([
  "genre",
  "author",
  "language",
  "max_budget_vnd",
])

export const wireMeResponseSchema = z.strictObject({
  principal_id: nonEmptyString(160),
  tenant_id: identifierSchema,
  allowed_modes: z.array(conversationModeSchema).check(
    z.refine(unique, { error: "allowed modes must be unique" }),
  ),
  store_id: identifierSchema,
})

export const wireConversationCreateRequestSchema = z.strictObject({
  mode: conversationModeSchema,
})

export const wireConversationSummarySchema = z.strictObject({
  conversation_id: identifierSchema,
  mode: conversationModeSchema,
  store_id: identifierSchema,
  title: z.nullable(nonEmptyString(160)),
  created_at: awareDatetimeSchema,
  updated_at: awareDatetimeSchema,
}).check(
  z.refine(
    (value) => dateDoesNotPrecede(value.updated_at, value.created_at),
    { error: "updated_at cannot precede created_at" },
  ),
)

export const wireConversationCreateResponseSchema = z.strictObject({
  conversation: wireConversationSummarySchema,
})

export const wireConversationListResponseSchema = z.strictObject({
  conversations: z.array(wireConversationSummarySchema),
  next_cursor: z.nullable(nonEmptyString(256)),
})

export const wireChatRequestSchema = z.strictObject({
  conversation_id: identifierSchema,
  client_turn_id: z.string().check(z.regex(clientTurnIdPattern)),
  message: nonEmptyString(MAX_V2_MESSAGE_LENGTH).check(
    z.refine((value) => value.trim().length > 0, {
      error: "message must contain non-whitespace text",
    }),
  ),
})

export const wireSafeExecutionErrorSchema = z.strictObject({
  code: actionNameSchema,
  message: nonEmptyString(500),
  retryable: z.boolean(),
})

export const wireExecutionRecordSchema = z.strictObject({
  execution_id: identifierSchema,
  step_id: identifierSchema,
  operation_key: nonEmptyString(256).check(z.minLength(8)),
  capability: actionNameSchema,
  service: identifierSchema,
  status: taskStatusSchema,
  started_at: z.nullable(awareDatetimeSchema),
  completed_at: z.nullable(awareDatetimeSchema),
  duration_ms: nonnegativeNumberSchema,
  reused: z.boolean(),
  error: z.nullable(wireSafeExecutionErrorSchema),
}).check(
  z.refine(
    (value) => value.started_at === null || value.completed_at === null ||
      dateDoesNotPrecede(value.completed_at, value.started_at),
    { error: "completed_at cannot precede started_at" },
  ),
  z.refine(
    (value) => value.status !== "failed" || value.error !== null,
    { error: "failed execution must include a safe error" },
  ),
  z.refine(
    (value) => value.status !== "success" || value.error === null,
    { error: "successful execution cannot include an error" },
  ),
)

export const wirePlanStepSchema = z.strictObject({
  step_id: identifierSchema,
  operation_key: nonEmptyString(256).check(z.minLength(8)),
  capability: actionNameSchema,
  service: identifierSchema,
  depends_on: z.array(identifierSchema).check(
    z.refine(unique, { error: "referenced IDs must be unique" }),
  ),
  data_version_ids: z.array(identifierSchema).check(
    z.refine(unique, { error: "referenced IDs must be unique" }),
  ),
})

export const wirePlanRevisionSchema = z.strictObject({
  revision: z.int().check(z.nonnegative(), z.maximum(MAX_V2_PLAN_REVISIONS)),
  reason: nonEmptyString(300),
  steps: z.array(wirePlanStepSchema).check(z.maxLength(MAX_V2_EXPERT_STEPS)),
  added_read_step_ids: z.array(identifierSchema).check(
    z.maxLength(MAX_V2_ADDED_READ_STEPS),
  ),
  executed_step_ids: z.array(identifierSchema),
  reused_step_ids: z.array(identifierSchema),
}).check(
  z.refine((revision) => unique(revision.steps.map((step) => step.step_id)), {
    error: "plan step IDs must be unique",
  }),
  z.refine((revision) => {
    const known = new Set(revision.steps.map((step) => step.step_id))
    return [
      revision.added_read_step_ids,
      revision.executed_step_ids,
      revision.reused_step_ids,
    ].every((references) => unique(references) && references.every((id) => known.has(id)))
  }, { error: "step IDs must uniquely reference plan steps" }),
  z.refine((revision) => {
    const executed = new Set(revision.executed_step_ids)
    return revision.reused_step_ids.every((id) => !executed.has(id))
  }, { error: "executed and reused step IDs must be disjoint" }),
  z.refine((revision) => {
    const seen = new Set<string>()
    for (const step of revision.steps) {
      if (!step.depends_on.every((id) => seen.has(id))) return false
      seen.add(step.step_id)
    }
    return true
  }, { error: "plan step has unresolved dependencies" }),
)

export const wirePlanTraceSchema = z.strictObject({
  plan_id: identifierSchema,
  intent: actionNameSchema,
  revisions: z.array(wirePlanRevisionSchema).check(
    z.minLength(1),
    z.maxLength(MAX_V2_PLAN_REVISIONS + 1),
  ),
}).check(
  z.refine((plan) => plan.revisions.every(
    (revision, index) => revision.revision === index,
  ), { error: "plan revisions must be consecutive and start at zero" }),
  z.refine((plan) => validatePlanTrace(plan), {
    error: "plan revisions contain invalid step reuse",
  }),
)

function validatePlanTrace(plan: z.infer<typeof wirePlanTraceSchema>): boolean {
  const definitions = new Map<string, string>()
  const operationOwners = new Map<string, string>()
  const executed = new Set<string>()
  let priorSteps = new Set<string>()
  const addedReads = new Set<string>()

  for (const [index, revision] of plan.revisions.entries()) {
    const currentSteps = new Set(revision.steps.map((step) => step.step_id))
    const added = new Set(revision.added_read_step_ids)
    if (index === 0 && added.size > 0) return false
    if (index > 0) {
      if (![...priorSteps].every((stepId) => currentSteps.has(stepId))) return false
      const newSteps = [...currentSteps].filter((stepId) => !priorSteps.has(stepId))
      if (newSteps.length !== added.size || newSteps.some((stepId) => !added.has(stepId))) {
        return false
      }
    }

    for (const step of revision.steps) {
      const definition = JSON.stringify([
        step.operation_key,
        step.capability,
        step.service,
        step.depends_on,
        step.data_version_ids,
      ])
      if (definitions.has(step.step_id) && definitions.get(step.step_id) !== definition) {
        return false
      }
      definitions.set(step.step_id, definition)
      if (operationOwners.has(step.operation_key) &&
        operationOwners.get(step.operation_key) !== step.step_id) {
        return false
      }
      operationOwners.set(step.operation_key, step.step_id)
    }

    if (revision.executed_step_ids.some((stepId) => executed.has(stepId))) return false
    if (revision.reused_step_ids.some((stepId) => !executed.has(stepId))) return false
    revision.executed_step_ids.forEach((stepId) => executed.add(stepId))
    revision.added_read_step_ids.forEach((stepId) => addedReads.add(stepId))
    priorSteps = currentSteps
  }

  const knowledgeSteps = new Set(
    plan.revisions.flatMap((revision) => revision.steps)
      .filter((step) => step.capability === "knowledge.retrieve")
      .map((step) => step.step_id),
  )
  return definitions.size <= MAX_V2_EXPERT_STEPS &&
    addedReads.size <= MAX_V2_ADDED_READ_STEPS &&
    knowledgeSteps.size <= MAX_V2_KNOWLEDGE_RETRIEVALS
}

export const wireEvidenceReferenceSchema = z.strictObject({
  evidence_id: identifierSchema,
  source_id: identifierSchema,
  source_version_id: identifierSchema,
  chunk_id: z.nullable(identifierSchema),
  span_id: z.nullable(identifierSchema),
  display_label: z.string().check(z.regex(displayCitationPattern)),
  kind: evidenceKindSchema,
  title: nonEmptyString(300),
  url: z.nullable(httpUrlSchema),
  observed_at: awareDatetimeSchema,
}).check(
  z.refine((evidence) => evidence.span_id === null || evidence.chunk_id !== null, {
    error: "span evidence must identify its chunk",
  }),
)

export const wireCitationSchema = z.strictObject({
  citation_id: identifierSchema,
  claim_id: identifierSchema,
  evidence_id: identifierSchema,
  span_id: z.nullable(identifierSchema),
  display_label: z.string().check(z.regex(displayCitationPattern)),
})

export const wireClaimSchema = z.strictObject({
  claim_id: identifierSchema,
  text: nonEmptyString(1_000),
  citation_ids: z.array(identifierSchema).check(
    z.minLength(1),
    z.refine(unique, { error: "claim citation IDs must be unique" }),
  ),
})

export const wireActionChangeSchema = z.strictObject({
  resource_type: z.enum(["cart_item", "order", "offer"]),
  resource_id: identifierSchema,
  field: z.enum(["quantity", "price_vnd", "status"]),
  before_integer: z.nullable(nonnegativeIntegerSchema),
  after_integer: z.nullable(nonnegativeIntegerSchema),
  before_text: z.nullable(nonEmptyString(80)),
  after_text: z.nullable(nonEmptyString(80)),
}).check(
  z.refine((change) => Number(change.before_integer !== null) +
    Number(change.before_text !== null) <= 1, {
    error: "an action change can have at most one typed before value",
  }),
  z.refine((change) => Number(change.after_integer !== null) +
    Number(change.after_text !== null) === 1, {
    error: "an action change must have one typed after value",
  }),
  z.refine((change) => change.field === "status"
    ? change.after_text !== null
    : change.after_integer !== null, {
    error: "the action field and after value types must match",
  }),
  z.refine((change) => change.field !== "price_vnd" || change.after_integer !== 0, {
    error: "price_vnd must be positive",
  }),
)

export const wireActionTargetSchema = z.strictObject({
  resource_type: z.enum(["cart", "order", "offer"]),
  resource_id: identifierSchema,
  expected_resource_version: resourceVersionSchema,
  data_version_ids: z.array(identifierSchema).check(
    z.minLength(1),
    z.refine(unique, { error: "action data version IDs must be unique" }),
  ),
})

export const wireActionCardSchema = z.strictObject({
  action_id: identifierSchema,
  proposal_id: identifierSchema,
  proposal_version: resourceVersionSchema,
  kind: actionKindSchema,
  status: actionStatusSchema,
  title: nonEmptyString(160),
  required_permission: actionNameSchema,
  confirmation_required: z.boolean(),
  target: wireActionTargetSchema,
  changes: z.array(wireActionChangeSchema).check(z.minLength(1)),
  expires_at: awareDatetimeSchema,
}).check(
  z.refine((card) => ![
    "checkout",
    "merchant_price_change",
    "merchant_inventory_change",
  ].includes(card.kind) || card.confirmation_required, {
    error: "this action kind requires explicit confirmation",
  }),
)

export const wireArtifactProductSchema = z.strictObject({
  product_id: positiveIntegerSchema,
  title: nonEmptyString(300),
  author: z.nullable(nonEmptyString(160)),
  price_vnd: z.nullable(priceVndSchema),
  rating: z.nullable(z.number().check(z.nonnegative(), z.maximum(5))),
  catalog_version_id: identifierSchema,
})

export const wireArtifactLineItemSchema = z.strictObject({
  product_id: positiveIntegerSchema,
  title: nonEmptyString(300),
  quantity: z.int().check(z.minimum(1), z.maximum(1_000_000)),
  unit_price_vnd: priceVndSchema,
  line_total_vnd: artifactAmountVndSchema,
}).check(
  z.refine((line) => line.line_total_vnd === line.quantity * line.unit_price_vnd, {
    error: "artifact line total must equal quantity times unit price",
  }),
)

const artifactBaseShape = {
  artifact_id: identifierSchema,
  resource_id: identifierSchema,
  resource_version: resourceVersionSchema,
  title: nonEmptyString(160),
}

export const wireProductComparisonArtifactSchema = z.strictObject({
  ...artifactBaseShape,
  kind: z.literal("product_comparison"),
  status: z.enum(["ready", "partial"]),
  products: z.array(wireArtifactProductSchema).check(
    z.minLength(2),
    z.maxLength(MAX_V2_CANDIDATES),
  ),
  data_version_ids: z.array(identifierSchema).check(
    z.minLength(1),
    z.maxLength(MAX_V2_ARTIFACT_DATA_VERSIONS),
  ),
}).check(
  z.refine((artifact) => unique(artifact.products.map((product) => product.product_id)), {
    error: "comparison artifact product IDs must be unique",
  }),
  z.refine((artifact) => unique(artifact.data_version_ids), {
    error: "comparison artifact data versions must be unique",
  }),
  z.refine((artifact) => {
    const versions = new Set(artifact.data_version_ids)
    return artifact.products.every((product) => versions.has(product.catalog_version_id))
  }, { error: "comparison product references an unknown data version" }),
)

function validArtifactLines(
  items: readonly z.infer<typeof wireArtifactLineItemSchema>[],
  total: number,
): boolean {
  return items.reduce((sum, item) => sum + item.line_total_vnd, 0) === total
}

export const wireCartArtifactSchema = z.strictObject({
  ...artifactBaseShape,
  kind: z.literal("cart"),
  status: z.enum(["active", "checked_out", "abandoned"]),
  items: z.array(wireArtifactLineItemSchema).check(z.maxLength(MAX_V2_ARTIFACT_ITEMS)),
  total_price_vnd: artifactAmountVndSchema,
  data_version_ids: z.array(identifierSchema).check(
    z.minLength(1),
    z.maxLength(MAX_V2_ARTIFACT_DATA_VERSIONS),
  ),
}).check(
  z.refine((artifact) => validArtifactLines(artifact.items, artifact.total_price_vnd), {
    error: "artifact total must equal the sum of its line totals",
  }),
  z.refine((artifact) => unique(artifact.data_version_ids), {
    error: "artifact data versions must be unique",
  }),
)

export const wireOrderArtifactSchema = z.strictObject({
  ...artifactBaseShape,
  kind: z.literal("order"),
  status: z.literal("confirmed"),
  cart_id: identifierSchema,
  cart_version: resourceVersionSchema,
  items: z.array(wireArtifactLineItemSchema).check(
    z.minLength(1),
    z.maxLength(MAX_V2_ARTIFACT_ITEMS),
  ),
  total_price_vnd: artifactAmountVndSchema,
  data_version_ids: z.array(identifierSchema).check(
    z.minLength(1),
    z.maxLength(MAX_V2_ARTIFACT_DATA_VERSIONS),
  ),
  created_at: awareDatetimeSchema,
}).check(
  z.refine((artifact) => validArtifactLines(artifact.items, artifact.total_price_vnd), {
    error: "artifact total must equal the sum of its line totals",
  }),
  z.refine((artifact) => unique(artifact.data_version_ids), {
    error: "artifact data versions must be unique",
  }),
)

export const wireActionArtifactSchema = z.strictObject({
  ...artifactBaseShape,
  kind: z.literal("action"),
  action_id: identifierSchema,
  proposal_id: identifierSchema,
  proposal_version: resourceVersionSchema,
  action_kind: actionKindSchema,
  status: actionStatusSchema,
})

export const wireTurnArtifactSchema = z.discriminatedUnion("kind", [
  wireProductComparisonArtifactSchema,
  wireCartArtifactSchema,
  wireOrderArtifactSchema,
  wireActionArtifactSchema,
])

export const wireUsageSummarySchema = z.strictObject({
  input_tokens: nonnegativeIntegerSchema,
  cached_input_tokens: nonnegativeIntegerSchema,
  output_tokens: nonnegativeIntegerSchema,
  reasoning_tokens: nonnegativeIntegerSchema,
  total_tokens: nonnegativeIntegerSchema,
  generation_calls: z.int().check(z.nonnegative(), z.maximum(10)),
  provider_attempts: z.int().check(z.nonnegative(), z.maximum(16)),
  knowledge_retrievals: z.int().check(
    z.nonnegative(),
    z.maximum(MAX_V2_KNOWLEDGE_RETRIEVALS),
  ),
  draft_repairs: z.int().check(z.nonnegative(), z.maximum(MAX_V2_DRAFT_REPAIRS)),
  estimated_cost_usd: decimalStringSchema,
  known_cost_usd: decimalStringSchema,
  reserved_cost_usd: decimalStringSchema,
  unknown_reserved_cost_usd: decimalStringSchema,
  unknown_usage_attempts: nonnegativeIntegerSchema,
  fallback_used: z.boolean(),
}).check(
  z.refine((usage) => usage.total_tokens === usage.input_tokens + usage.output_tokens, {
    error: "total_tokens must equal input_tokens plus output_tokens",
  }),
  z.refine((usage) => usage.cached_input_tokens <= usage.input_tokens, {
    error: "cached input tokens cannot exceed input tokens",
  }),
  z.refine((usage) => usage.reasoning_tokens <= usage.output_tokens, {
    error: "reasoning tokens cannot exceed output tokens",
  }),
  z.refine((usage) => usage.unknown_usage_attempts <= usage.provider_attempts, {
    error: "unknown usage attempts cannot exceed provider attempts",
  }),
  z.refine((usage) => decimalEqualsSum(usage.estimated_cost_usd, [
    usage.known_cost_usd,
    usage.reserved_cost_usd,
    usage.unknown_reserved_cost_usd,
  ]), { error: "estimated cost must equal all cost components" }),
)

export const wireTurnResultSchema = z.strictObject({
  outcome: dialogueOutcomeSchema,
  answer: nonEmptyString(20_000),
  claims: z.array(wireClaimSchema),
  citations: z.array(wireCitationSchema),
  evidence: z.array(wireEvidenceReferenceSchema),
  action_cards: z.array(wireActionCardSchema),
  artifacts: z.array(wireTurnArtifactSchema).check(z.maxLength(MAX_V2_ARTIFACTS)),
  plan: z.nullable(wirePlanTraceSchema),
  executions: z.array(wireExecutionRecordSchema).check(z.maxLength(MAX_V2_EXPERT_STEPS)),
  warnings: z.array(z.string()),
}).check(
  z.refine(validateTurnResultReferences, { error: "turn result references are invalid" }),
)

function validateTurnResultReferences(result: z.infer<typeof wireTurnResultSchema>): boolean {
  const claimIds = result.claims.map((claim) => claim.claim_id)
  const citationIds = result.citations.map((citation) => citation.citation_id)
  const evidenceIds = result.evidence.map((evidence) => evidence.evidence_id)
  const artifactIds = result.artifacts.map((artifact) => artifact.artifact_id)
  const actionIds = result.action_cards.map((card) => card.action_id)
  const displayLabels = result.evidence.map((evidence) => evidence.display_label)
  if (![claimIds, citationIds, evidenceIds, artifactIds, actionIds, displayLabels].every(unique)) {
    return false
  }

  const citations = new Map(result.citations.map((citation) => [citation.citation_id, citation]))
  const claims = new Map(result.claims.map((claim) => [claim.claim_id, claim]))
  const evidence = new Map(result.evidence.map((item) => [item.evidence_id, item]))
  for (const claim of result.claims) {
    for (const citationId of claim.citation_ids) {
      const citation = citations.get(citationId)
      if (!citation || citation.claim_id !== claim.claim_id) return false
    }
  }
  for (const citation of result.citations) {
    const claim = claims.get(citation.claim_id)
    const item = evidence.get(citation.evidence_id)
    if (!claim?.citation_ids.includes(citation.citation_id) || !item) return false
    if (citation.span_id !== null && citation.span_id !== item.span_id) return false
    if (citation.display_label !== item.display_label) return false
  }
  if (result.outcome === "awaiting_confirmation" && result.action_cards.length === 0) {
    return false
  }
  const actions = new Map(result.action_cards.map((card) => [card.action_id, card]))
  for (const artifact of result.artifacts) {
    if (artifact.kind !== "action") continue
    const card = actions.get(artifact.action_id)
    if (!card || artifact.proposal_id !== card.proposal_id ||
      artifact.proposal_version !== card.proposal_version ||
      artifact.action_kind !== card.kind || artifact.status !== card.status ||
      artifact.resource_id !== card.target.resource_id ||
      artifact.resource_version !== card.target.expected_resource_version ||
      artifact.title !== card.title) {
      return false
    }
  }
  return true
}

const terminalTurnStatuses = new Set(["completed", "failed", "cancelled", "interrupted"])

export const wireHistoryTurnSchema = z.strictObject({
  turn_id: identifierSchema,
  client_turn_id: z.string().check(z.regex(clientTurnIdPattern)),
  status: turnStatusSchema,
  outcome: z.nullable(dialogueOutcomeSchema),
  user_message: z.nullable(z.string().check(z.maxLength(MAX_V2_MESSAGE_LENGTH))),
  assistant_result: z.nullable(wireTurnResultSchema),
  error: z.nullable(wireSafeExecutionErrorSchema),
  action_cards: z.array(wireActionCardSchema),
  created_at: awareDatetimeSchema,
  completed_at: z.nullable(awareDatetimeSchema),
}).check(
  z.refine((turn) => turn.status === "completed"
    ? turn.assistant_result !== null && turn.outcome !== null && turn.error === null &&
      turn.outcome === turn.assistant_result.outcome &&
      sameJson(turn.action_cards, turn.assistant_result.action_cards)
    : turn.assistant_result === null && turn.outcome === null, {
    error: "history result does not match its lifecycle state",
  }),
  z.refine((turn) => ["failed", "interrupted"].includes(turn.status)
    ? turn.error !== null
    : turn.error === null, { error: "history error does not match its lifecycle state" }),
  z.refine((turn) => terminalTurnStatuses.has(turn.status)
    ? turn.completed_at !== null
    : turn.completed_at === null, {
    error: "history completion time does not match its lifecycle state",
  }),
  z.refine((turn) => turn.completed_at === null ||
    dateDoesNotPrecede(turn.completed_at, turn.created_at), {
    error: "history completion cannot precede creation",
  }),
)

export const wireTurnSummarySchema = z.strictObject({
  turn_id: identifierSchema,
  client_turn_id: z.string().check(z.regex(clientTurnIdPattern)),
  status: turnStatusSchema,
  outcome: z.nullable(dialogueOutcomeSchema),
  created_at: awareDatetimeSchema,
  completed_at: z.nullable(awareDatetimeSchema),
}).check(
  z.refine((turn) => turn.status === "completed"
    ? turn.outcome !== null
    : turn.outcome === null, { error: "turn outcome does not match its lifecycle state" }),
  z.refine((turn) => terminalTurnStatuses.has(turn.status)
    ? turn.completed_at !== null
    : turn.completed_at === null, {
    error: "turn completion time does not match its lifecycle state",
  }),
  z.refine((turn) => turn.completed_at === null ||
    dateDoesNotPrecede(turn.completed_at, turn.created_at), {
    error: "completed_at cannot precede created_at",
  }),
)

export const wireTurnResponseSchema = z.strictObject({
  conversation_id: identifierSchema,
  turn: wireTurnSummarySchema,
  request_id: identifierSchema,
  trace_id: identifierSchema,
  result: z.nullable(wireTurnResultSchema),
  error: z.nullable(wireSafeExecutionErrorSchema),
  usage: wireUsageSummarySchema,
}).check(
  z.refine((response) => response.turn.status === "completed"
    ? response.result !== null
    : response.result === null, { error: "turn result does not match its lifecycle state" }),
  z.refine((response) => ["failed", "interrupted"].includes(response.turn.status)
    ? response.error !== null
    : response.error === null, { error: "turn error does not match its lifecycle state" }),
  z.refine((response) => response.result === null ||
    response.result.outcome === response.turn.outcome, {
    error: "turn and result outcomes must match",
  }),
)

export const wireChatResponseSchema = wireTurnResponseSchema

const turnSseEnvelopeShape = {
  sequence: positiveIntegerSchema,
  request_id: identifierSchema,
  trace_id: identifierSchema,
  turn_id: identifierSchema,
}

export const wireTurnSseProgressEventSchema = z.strictObject({
  ...turnSseEnvelopeShape,
  event: z.literal("progress"),
  phase: turnSseProgressPhaseSchema,
  turn_status: turnStatusSchema,
  step_id: z.nullable(identifierSchema),
  capability: z.nullable(actionNameSchema),
  plan_revision: z.nullable(
    z.int().check(z.nonnegative(), z.maximum(MAX_V2_PLAN_REVISIONS)),
  ),
  step_status: z.nullable(taskStatusSchema),
  reused: z.boolean(),
}).check(
  z.refine((event) => {
    const stepPhase = ["step_started", "step_finished"].includes(event.phase)
    return stepPhase === (event.step_id !== null) &&
      stepPhase === (event.capability !== null) &&
      stepPhase === (event.plan_revision !== null)
  }, { error: "step progress must carry exactly the step fields" }),
  z.refine((event) => event.phase === "step_finished"
    ? event.step_status !== null && ["success", "partial_success", "failed"].includes(event.step_status)
    : event.step_status === null, { error: "step status does not match the progress phase" }),
  z.refine((event) => !["step_started", "step_finished"].includes(event.phase) ||
    event.turn_status === "running", { error: "step progress requires a running turn" }),
  z.refine((event) => event.phase !== "admitted" || event.turn_status === "pending", {
    error: "admitted progress requires a pending turn",
  }),
  z.refine((event) => event.phase !== "attached" ||
    ["pending", "running"].includes(event.turn_status), {
    error: "attached progress requires a live turn",
  }),
  z.refine((event) => event.phase !== "claimed" || event.turn_status === "running", {
    error: "claimed progress requires a running turn",
  }),
  z.refine((event) => event.phase === "step_finished" || !event.reused, {
    error: "only finished step progress can be reused",
  }),
)

export const wireTurnSseTextDeltaEventSchema = z.strictObject({
  ...turnSseEnvelopeShape,
  event: z.literal("text_delta"),
  turn_status: z.literal("completed"),
  delta: nonEmptyString(MAX_V2_TEXT_DELTA_LENGTH),
  post_grounding: z.literal(true),
})

export const wireTurnCompletedTerminalSchema = z.strictObject({
  status: z.literal("completed"),
  result: wireTurnResultSchema,
  usage: wireUsageSummarySchema,
})
export const wireTurnFailedTerminalSchema = z.strictObject({
  status: z.literal("failed"),
  error: wireSafeExecutionErrorSchema,
  usage: wireUsageSummarySchema,
})
export const wireTurnCancelledTerminalSchema = z.strictObject({
  status: z.literal("cancelled"),
  usage: wireUsageSummarySchema,
})
export const wireTurnInterruptedTerminalSchema = z.strictObject({
  status: z.literal("interrupted"),
  error: wireSafeExecutionErrorSchema,
  usage: wireUsageSummarySchema,
})
export const wireTurnTerminalPayloadSchema = z.discriminatedUnion("status", [
  wireTurnCompletedTerminalSchema,
  wireTurnFailedTerminalSchema,
  wireTurnCancelledTerminalSchema,
  wireTurnInterruptedTerminalSchema,
])

export const wireTurnSseTerminalEventSchema = z.strictObject({
  ...turnSseEnvelopeShape,
  event: z.literal("terminal"),
  payload: wireTurnTerminalPayloadSchema,
  reused_result: z.boolean(),
  server_settled: z.boolean(),
})

export const wireTurnSseEventSchema = z.discriminatedUnion("event", [
  wireTurnSseProgressEventSchema,
  wireTurnSseTextDeltaEventSchema,
  wireTurnSseTerminalEventSchema,
])

export const wireTurnSseSequenceSchema = z.strictObject({
  events: z.array(wireTurnSseEventSchema).check(
    z.minLength(1),
    z.maxLength(MAX_V2_SSE_EVENTS),
  ),
}).check(
  z.refine((sequence) => sequence.events.every(
    (event, index) => index === 0 || event.sequence > sequence.events[index - 1].sequence,
  ), { error: "SSE event sequence must be strictly increasing" }),
  z.refine((sequence) => {
    const first = sequence.events[0]
    return sequence.events.every((event) => event.request_id === first.request_id &&
      event.trace_id === first.trace_id && event.turn_id === first.turn_id)
  }, { error: "SSE events must share request, trace, and turn IDs" }),
  z.refine((sequence) => sequence.events.every((event, index) =>
    event.event !== "terminal" || index === sequence.events.length - 1) &&
    sequence.events.at(-1)?.event === "terminal", {
    error: "a completed SSE sequence requires one final terminal event",
  }),
)

export const wireConversationDetailResponseSchema = z.strictObject({
  conversation: wireConversationSummarySchema,
  turns: z.array(wireHistoryTurnSchema),
})

export const wireActionConfirmRequestSchema = z.strictObject({
  proposal_version: resourceVersionSchema,
})
export const wireActionRejectRequestSchema = z.strictObject({
  proposal_version: resourceVersionSchema,
  reason: z.optional(z.nullable(nonEmptyString(300))),
})
export const wireActionDecisionResponseSchema = z.strictObject({
  action_id: identifierSchema,
  proposal_version: resourceVersionSchema,
  status: actionStatusSchema,
  decided_at: awareDatetimeSchema,
  reused_result: z.boolean(),
})
export const wireActionExecutionResponseSchema = z.strictObject({
  action_id: identifierSchema,
  status: z.enum(["executed", "expired", "conflicted", "failed"]),
  resource_id: identifierSchema,
  resource_version: resourceVersionSchema,
  reused_result: z.boolean(),
})
export const wireActionResultSchema = z.union([
  wireActionExecutionResponseSchema,
  wireActionDecisionResponseSchema,
])
export const wireActionReadResponseSchema = z.strictObject({
  action: wireActionCardSchema,
  result: z.nullable(wireActionResultSchema),
}).check(
  z.refine((response) => !["proposed", "confirmed"].includes(response.action.status) ||
    response.result === null, { error: "pending actions cannot expose a terminal result" }),
  z.refine((response) => {
    if (response.result === null) return true
    if ("decided_at" in response.result && !["rejected", "expired"].includes(response.result.status)) {
      return false
    }
    return response.result.action_id === response.action.action_id &&
      response.result.status === response.action.status
  }, { error: "action result does not match the action card" }),
)

export const wireGenrePreferenceSchema = z.strictObject({
  kind: z.literal("genre"),
  value: nonEmptyString(100),
})
export const wireAuthorPreferenceSchema = z.strictObject({
  kind: z.literal("author"),
  value: nonEmptyString(160),
})
export const wireLanguagePreferenceSchema = z.strictObject({
  kind: z.literal("language"),
  value: z.string().check(z.minLength(2), z.maxLength(40)),
})
export const wireBudgetPreferenceSchema = z.strictObject({
  kind: z.literal("max_budget_vnd"),
  value: priceVndSchema,
})
export const wirePreferenceValueSchema = z.discriminatedUnion("kind", [
  wireGenrePreferenceSchema,
  wireAuthorPreferenceSchema,
  wireLanguagePreferenceSchema,
  wireBudgetPreferenceSchema,
])
export const wirePreferencePutRequestSchema = z.strictObject({
  source_turn_id: identifierSchema,
  preference: wirePreferenceValueSchema,
})
export const wirePreferenceDeleteRequestSchema = z.strictObject({
  preference_id: identifierSchema,
})
export const wirePreferenceRecordSchema = z.strictObject({
  preference_id: identifierSchema,
  source_turn_id: identifierSchema,
  preference: wirePreferenceValueSchema,
  created_at: awareDatetimeSchema,
  updated_at: awareDatetimeSchema,
})
export const wirePreferenceListResponseSchema = z.strictObject({
  preferences: z.array(wirePreferenceRecordSchema),
})

export const wireSafeErrorDetailSchema = z.strictObject({
  code: z.string(),
  message: z.string(),
  request_id: z.string(),
  trace_id: z.string(),
  retryable: z.boolean(),
  validation_errors: z.array(z.record(z.string(), z.unknown())),
})
export const wireSafeErrorResponseSchema = z.strictObject({
  error: wireSafeErrorDetailSchema,
})

type SnakeToCamel<Value extends string> =
  Value extends `${infer Head}_${infer Tail}`
    ? `${Head}${Capitalize<SnakeToCamel<Tail>>}`
    : Value

export type Camelize<Value> =
  Value extends readonly (infer Item)[]
    ? Camelize<Item>[]
    : Value extends object
      ? { [Key in keyof Value as Key extends string ? SnakeToCamel<Key> : Key]: Camelize<Value[Key]> }
      : Value

function camelizeKeys<Value>(value: Value): Camelize<Value> {
  if (Array.isArray(value)) {
    return value.map((item) => camelizeKeys(item)) as Camelize<Value>
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value).map(([key, item]) => [
      key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase()),
      camelizeKeys(item),
    ])
    return Object.fromEntries(entries) as Camelize<Value>
  }
  return value as Camelize<Value>
}

export const meResponseSchema = z.pipe(
  wireMeResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const conversationCreateRequestSchema = z.pipe(
  wireConversationCreateRequestSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const conversationSummarySchema = z.pipe(
  wireConversationSummarySchema,
  z.transform((value) => camelizeKeys(value)),
)
export const conversationCreateResponseSchema = z.pipe(
  wireConversationCreateResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const conversationListResponseSchema = z.pipe(
  wireConversationListResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const chatRequestSchema = z.pipe(
  wireChatRequestSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const safeExecutionErrorSchema = z.pipe(
  wireSafeExecutionErrorSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const executionRecordSchema = z.pipe(
  wireExecutionRecordSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const planStepSchema = z.pipe(
  wirePlanStepSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const planRevisionSchema = z.pipe(
  wirePlanRevisionSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const planTraceSchema = z.pipe(
  wirePlanTraceSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const evidenceReferenceSchema = z.pipe(
  wireEvidenceReferenceSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const citationSchema = z.pipe(
  wireCitationSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const claimSchema = z.pipe(
  wireClaimSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionChangeSchema = z.pipe(
  wireActionChangeSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionTargetSchema = z.pipe(
  wireActionTargetSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionCardSchema = z.pipe(
  wireActionCardSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const artifactProductSchema = z.pipe(
  wireArtifactProductSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const artifactLineItemSchema = z.pipe(
  wireArtifactLineItemSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const productComparisonArtifactSchema = z.pipe(
  wireProductComparisonArtifactSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const cartArtifactSchema = z.pipe(
  wireCartArtifactSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const orderArtifactSchema = z.pipe(
  wireOrderArtifactSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionArtifactSchema = z.pipe(
  wireActionArtifactSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnArtifactSchema = z.pipe(
  wireTurnArtifactSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const usageSummarySchema = z.pipe(
  wireUsageSummarySchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnResultSchema = z.pipe(
  wireTurnResultSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const historyTurnSchema = z.pipe(
  wireHistoryTurnSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnSummarySchema = z.pipe(
  wireTurnSummarySchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnResponseSchema = z.pipe(
  wireTurnResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const chatResponseSchema = turnResponseSchema
export const turnSseProgressEventSchema = z.pipe(
  wireTurnSseProgressEventSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnSseTextDeltaEventSchema = z.pipe(
  wireTurnSseTextDeltaEventSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnCompletedTerminalSchema = z.pipe(
  wireTurnCompletedTerminalSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnFailedTerminalSchema = z.pipe(
  wireTurnFailedTerminalSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnCancelledTerminalSchema = z.pipe(
  wireTurnCancelledTerminalSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnInterruptedTerminalSchema = z.pipe(
  wireTurnInterruptedTerminalSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnTerminalPayloadSchema = z.pipe(
  wireTurnTerminalPayloadSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnSseTerminalEventSchema = z.pipe(
  wireTurnSseTerminalEventSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnSseEventSchema = z.pipe(
  wireTurnSseEventSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const turnSseSequenceSchema = z.pipe(
  wireTurnSseSequenceSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const conversationDetailResponseSchema = z.pipe(
  wireConversationDetailResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionConfirmRequestSchema = z.pipe(
  wireActionConfirmRequestSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionRejectRequestSchema = z.pipe(
  wireActionRejectRequestSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionDecisionResponseSchema = z.pipe(
  wireActionDecisionResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionExecutionResponseSchema = z.pipe(
  wireActionExecutionResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionResultSchema = z.pipe(
  wireActionResultSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const actionReadResponseSchema = z.pipe(
  wireActionReadResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const genrePreferenceSchema = z.pipe(
  wireGenrePreferenceSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const authorPreferenceSchema = z.pipe(
  wireAuthorPreferenceSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const languagePreferenceSchema = z.pipe(
  wireLanguagePreferenceSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const budgetPreferenceSchema = z.pipe(
  wireBudgetPreferenceSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const preferenceValueSchema = z.pipe(
  wirePreferenceValueSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const preferencePutRequestSchema = z.pipe(
  wirePreferencePutRequestSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const preferenceDeleteRequestSchema = z.pipe(
  wirePreferenceDeleteRequestSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const preferenceRecordSchema = z.pipe(
  wirePreferenceRecordSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const preferenceListResponseSchema = z.pipe(
  wirePreferenceListResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const safeErrorDetailSchema = z.pipe(
  wireSafeErrorDetailSchema,
  z.transform((value) => camelizeKeys(value)),
)
export const safeErrorResponseSchema = z.pipe(
  wireSafeErrorResponseSchema,
  z.transform((value) => camelizeKeys(value)),
)

export type ConversationMode = z.infer<typeof conversationModeSchema>
export type DialogueOutcome = z.infer<typeof dialogueOutcomeSchema>
export type TurnStatus = z.infer<typeof turnStatusSchema>
export type TaskStatus = z.infer<typeof taskStatusSchema>
export type EvidenceKind = z.infer<typeof evidenceKindSchema>
export type ActionKind = z.infer<typeof actionKindSchema>
export type ActionStatus = z.infer<typeof actionStatusSchema>
export type ArtifactKind = z.infer<typeof artifactKindSchema>
export type TurnSseEventKind = z.infer<typeof turnSseEventKindSchema>
export type TurnSseProgressPhase = z.infer<typeof turnSseProgressPhaseSchema>
export type PreferenceKind = z.infer<typeof preferenceKindSchema>
export type MeResponse = z.infer<typeof meResponseSchema>
export type ConversationCreateRequest = z.infer<typeof conversationCreateRequestSchema>
export type ConversationSummary = z.infer<typeof conversationSummarySchema>
export type ConversationCreateResponse = z.infer<typeof conversationCreateResponseSchema>
export type ConversationListResponse = z.infer<typeof conversationListResponseSchema>
export type ChatRequest = z.infer<typeof chatRequestSchema>
export type SafeExecutionError = z.infer<typeof safeExecutionErrorSchema>
export type ExecutionRecord = z.infer<typeof executionRecordSchema>
export type PlanStep = z.infer<typeof planStepSchema>
export type PlanRevision = z.infer<typeof planRevisionSchema>
export type PlanTrace = z.infer<typeof planTraceSchema>
export type EvidenceReference = z.infer<typeof evidenceReferenceSchema>
export type Citation = z.infer<typeof citationSchema>
export type Claim = z.infer<typeof claimSchema>
export type ActionChange = z.infer<typeof actionChangeSchema>
export type ActionTarget = z.infer<typeof actionTargetSchema>
export type ActionCard = z.infer<typeof actionCardSchema>
export type ArtifactProduct = z.infer<typeof artifactProductSchema>
export type ArtifactLineItem = z.infer<typeof artifactLineItemSchema>
export type ProductComparisonArtifact = z.infer<typeof productComparisonArtifactSchema>
export type CartArtifact = z.infer<typeof cartArtifactSchema>
export type OrderArtifact = z.infer<typeof orderArtifactSchema>
export type ActionArtifact = z.infer<typeof actionArtifactSchema>
export type TurnArtifact = z.infer<typeof turnArtifactSchema>
export type UsageSummary = z.infer<typeof usageSummarySchema>
export type TurnResult = z.infer<typeof turnResultSchema>
export type HistoryTurn = z.infer<typeof historyTurnSchema>
export type TurnSummary = z.infer<typeof turnSummarySchema>
export type TurnResponse = z.infer<typeof turnResponseSchema>
export type ChatResponse = z.infer<typeof chatResponseSchema>
export type TurnSseProgressEvent = z.infer<typeof turnSseProgressEventSchema>
export type TurnSseTextDeltaEvent = z.infer<typeof turnSseTextDeltaEventSchema>
export type TurnCompletedTerminal = z.infer<typeof turnCompletedTerminalSchema>
export type TurnFailedTerminal = z.infer<typeof turnFailedTerminalSchema>
export type TurnCancelledTerminal = z.infer<typeof turnCancelledTerminalSchema>
export type TurnInterruptedTerminal = z.infer<typeof turnInterruptedTerminalSchema>
export type TurnTerminalPayload = z.infer<typeof turnTerminalPayloadSchema>
export type TurnSseTerminalEvent = z.infer<typeof turnSseTerminalEventSchema>
export type TurnSseEvent = z.infer<typeof turnSseEventSchema>
export type TurnSseSequence = z.infer<typeof turnSseSequenceSchema>
export type ConversationDetailResponse = z.infer<typeof conversationDetailResponseSchema>
export type ActionConfirmRequest = z.infer<typeof actionConfirmRequestSchema>
export type ActionRejectRequest = z.infer<typeof actionRejectRequestSchema>
export type ActionDecisionResponse = z.infer<typeof actionDecisionResponseSchema>
export type ActionExecutionResponse = z.infer<typeof actionExecutionResponseSchema>
export type ActionResult = z.infer<typeof actionResultSchema>
export type ActionReadResponse = z.infer<typeof actionReadResponseSchema>
export type PreferenceValue = z.infer<typeof preferenceValueSchema>
export type PreferencePutRequest = z.infer<typeof preferencePutRequestSchema>
export type PreferenceDeleteRequest = z.infer<typeof preferenceDeleteRequestSchema>
export type PreferenceRecord = z.infer<typeof preferenceRecordSchema>
export type PreferenceListResponse = z.infer<typeof preferenceListResponseSchema>
export type SafeErrorDetail = z.infer<typeof safeErrorDetailSchema>
export type SafeErrorResponse = z.infer<typeof safeErrorResponseSchema>
