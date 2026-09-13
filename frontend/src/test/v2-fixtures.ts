import {
  actionCardSchema,
  conversationSummarySchema,
  historyTurnSchema,
  preferenceRecordSchema,
  turnResponseSchema,
  turnResultSchema,
  turnSseEventSchema,
  usageSummarySchema,
} from "@/lib/v2-contracts"

export const v2Now = "2026-09-09T08:00:00Z"
export const v2CompletedAt = "2026-09-09T08:00:01Z"

export const v2UsageSummaryWire = {
  input_tokens: 20,
  cached_input_tokens: 5,
  output_tokens: 10,
  reasoning_tokens: 2,
  total_tokens: 30,
  generation_calls: 1,
  provider_attempts: 1,
  knowledge_retrievals: 1,
  draft_repairs: 0,
  estimated_cost_usd: "0.001",
  known_cost_usd: "0.001",
  reserved_cost_usd: "0",
  unknown_reserved_cost_usd: "0E-12",
  unknown_usage_attempts: 0,
  fallback_used: false,
} as const

export const v2UsageSummary = usageSummarySchema.parse(v2UsageSummaryWire)

export const v2ActionCardWire = {
  action_id: "action_checkout_001",
  proposal_id: "proposal_checkout_001",
  proposal_version: 1,
  kind: "checkout",
  status: "proposed",
  title: "Xác nhận đơn hàng sandbox",
  required_permission: "ecommerce.write",
  confirmation_required: true,
  target: {
    resource_type: "cart",
    resource_id: "cart_demo_001",
    expected_resource_version: 3,
    data_version_ids: ["version_catalog_001"],
  },
  changes: [
    {
      resource_type: "order",
      resource_id: "order_demo_001",
      field: "status",
      before_integer: null,
      after_integer: null,
      before_text: "cart_active",
      after_text: "confirmed",
    },
  ],
  expires_at: "2026-09-09T08:10:00Z",
} as const

export const v2ActionCard = actionCardSchema.parse(v2ActionCardWire)

export const v2ArtifactsWire = [
  {
    kind: "product_comparison",
    artifact_id: "artifact_comparison_001",
    resource_id: "comparison_demo_001",
    resource_version: 1,
    status: "ready",
    title: "So sánh sách",
    products: [
      {
        product_id: 42,
        title: "Sapiens",
        author: "Yuval Noah Harari",
        price_vnd: 100_000,
        rating: 4.7,
        catalog_version_id: "version_catalog_001",
      },
      {
        product_id: 7,
        title: "Homo Deus",
        author: "Yuval Noah Harari",
        price_vnd: 120_000,
        rating: 4.6,
        catalog_version_id: "version_catalog_001",
      },
    ],
    data_version_ids: ["version_catalog_001"],
  },
  {
    kind: "cart",
    artifact_id: "artifact_cart_001",
    resource_id: "cart_demo_001",
    resource_version: 3,
    status: "active",
    title: "Giỏ hàng sandbox",
    items: [
      {
        product_id: 42,
        title: "Sapiens",
        quantity: 2,
        unit_price_vnd: 100_000,
        line_total_vnd: 200_000,
      },
    ],
    total_price_vnd: 200_000,
    data_version_ids: ["version_catalog_001"],
  },
  {
    kind: "order",
    artifact_id: "artifact_order_001",
    resource_id: "order_demo_001",
    resource_version: 1,
    status: "confirmed",
    title: "Đơn hàng sandbox",
    cart_id: "cart_demo_001",
    cart_version: 3,
    items: [
      {
        product_id: 42,
        title: "Sapiens",
        quantity: 2,
        unit_price_vnd: 100_000,
        line_total_vnd: 200_000,
      },
    ],
    total_price_vnd: 200_000,
    data_version_ids: ["version_catalog_001"],
    created_at: v2Now,
  },
  {
    kind: "action",
    artifact_id: "artifact_action_001",
    resource_id: "cart_demo_001",
    resource_version: 3,
    title: "Xác nhận đơn hàng sandbox",
    action_id: "action_checkout_001",
    proposal_id: "proposal_checkout_001",
    proposal_version: 1,
    action_kind: "checkout",
    status: "proposed",
  },
] as const

export const v2TurnResultWire = {
  outcome: "awaiting_confirmation",
  answer: "Đã chuẩn bị đề xuất có kiểm chứng [C1].",
  claims: [
    {
      claim_id: "claim_demo_001",
      text: "Sapiens có trong snapshot danh mục.",
      citation_ids: ["citation_demo_001"],
    },
  ],
  citations: [
    {
      citation_id: "citation_demo_001",
      claim_id: "claim_demo_001",
      evidence_id: "evidence_demo_001",
      span_id: "span_demo_001",
      display_label: "[C1]",
    },
  ],
  evidence: [
    {
      evidence_id: "evidence_demo_001",
      source_id: "source_demo_001",
      source_version_id: "version_catalog_001",
      chunk_id: "chunk_demo_001",
      span_id: "span_demo_001",
      display_label: "[C1]",
      kind: "catalog",
      title: "Danh mục sách demo",
      url: "https://example.test/books/42",
      observed_at: v2Now,
    },
  ],
  action_cards: [v2ActionCardWire],
  artifacts: v2ArtifactsWire,
  plan: {
    plan_id: "plan_demo_001",
    intent: "checkout.prepare",
    revisions: [
      {
        revision: 0,
        reason: "initial plan",
        steps: [
          {
            step_id: "step_catalog_001",
            operation_key: "operation-key-catalog-001",
            capability: "product.catalog.search",
            service: "product",
            depends_on: [],
            data_version_ids: ["version_catalog_001"],
          },
        ],
        added_read_step_ids: [],
        executed_step_ids: ["step_catalog_001"],
        reused_step_ids: [],
      },
    ],
  },
  executions: [
    {
      execution_id: "execution_demo_001",
      step_id: "step_catalog_001",
      operation_key: "operation-key-catalog-001",
      capability: "product.catalog.search",
      service: "product",
      status: "success",
      started_at: v2Now,
      completed_at: v2CompletedAt,
      duration_ms: 12.4,
      reused: false,
      error: null,
    },
  ],
  warnings: [],
} as const

export const v2TurnResult = turnResultSchema.parse(v2TurnResultWire)

export const v2ConversationSummaryWire = {
  conversation_id: "conversation_demo_001",
  mode: "shopper",
  store_id: "demo",
  title: "Tư vấn sách lịch sử",
  created_at: v2Now,
  updated_at: v2CompletedAt,
} as const

export const v2ConversationSummary = conversationSummarySchema.parse(
  v2ConversationSummaryWire,
)

export const v2HistoryTurnWire = {
  turn_id: "turn_demo_001",
  client_turn_id: "browser:turn-1",
  status: "completed",
  outcome: "awaiting_confirmation",
  user_message: "Gợi ý sách lịch sử và chuẩn bị thanh toán",
  assistant_result: v2TurnResultWire,
  error: null,
  action_cards: [v2ActionCardWire],
  created_at: v2Now,
  completed_at: v2CompletedAt,
} as const

export const v2HistoryTurn = historyTurnSchema.parse(v2HistoryTurnWire)

export const v2CompletedTurnResponseWire = {
  conversation_id: "conversation_demo_001",
  turn: {
    turn_id: "turn_demo_001",
    client_turn_id: "browser:turn-1",
    status: "completed",
    outcome: "awaiting_confirmation",
    created_at: v2Now,
    completed_at: v2CompletedAt,
  },
  request_id: "request_v2_001",
  trace_id: "trace_v2_001",
  result: v2TurnResultWire,
  error: null,
  usage: v2UsageSummaryWire,
} as const

export const v2CompletedTurnResponse = turnResponseSchema.parse(
  v2CompletedTurnResponseWire,
)

export const v2RunningTurnResponseWire = {
  conversation_id: "conversation_demo_001",
  turn: {
    turn_id: "turn_demo_001",
    client_turn_id: "browser:turn-1",
    status: "running",
    outcome: null,
    created_at: v2Now,
    completed_at: null,
  },
  request_id: "request_v2_001",
  trace_id: "trace_v2_001",
  result: null,
  error: null,
  usage: {
    ...v2UsageSummaryWire,
    input_tokens: 0,
    cached_input_tokens: 0,
    output_tokens: 0,
    reasoning_tokens: 0,
    total_tokens: 0,
    generation_calls: 0,
    provider_attempts: 0,
    knowledge_retrievals: 0,
    estimated_cost_usd: "0",
    known_cost_usd: "0",
  },
} as const

export const v2RunningTurnResponse = turnResponseSchema.parse(
  v2RunningTurnResponseWire,
)

const v2Correlation = {
  request_id: "request_v2_001",
  trace_id: "trace_v2_001",
  turn_id: "turn_demo_001",
} as const

export const v2ProgressEventsWire = [
  {
    event: "progress",
    sequence: 1,
    ...v2Correlation,
    phase: "admitted",
    turn_status: "pending",
    step_id: null,
    capability: null,
    plan_revision: null,
    step_status: null,
    reused: false,
  },
  {
    event: "progress",
    sequence: 2,
    ...v2Correlation,
    phase: "claimed",
    turn_status: "running",
    step_id: null,
    capability: null,
    plan_revision: null,
    step_status: null,
    reused: false,
  },
  {
    event: "progress",
    sequence: 3,
    ...v2Correlation,
    phase: "step_started",
    turn_status: "running",
    step_id: "step_catalog_001",
    capability: "product.catalog.search",
    plan_revision: 0,
    step_status: null,
    reused: false,
  },
  {
    event: "progress",
    sequence: 4,
    ...v2Correlation,
    phase: "step_finished",
    turn_status: "running",
    step_id: "step_catalog_001",
    capability: "product.catalog.search",
    plan_revision: 0,
    step_status: "success",
    reused: false,
  },
] as const

export const v2TextDeltaEventWire = {
  event: "text_delta",
  sequence: 5,
  ...v2Correlation,
  turn_status: "completed",
  delta: "Đã chuẩn bị đề xuất có kiểm chứng [C1].",
  post_grounding: true,
} as const

export const v2TerminalEventWire = {
  event: "terminal",
  sequence: 6,
  ...v2Correlation,
  payload: {
    status: "completed",
    result: v2TurnResultWire,
    usage: v2UsageSummaryWire,
  },
  reused_result: false,
} as const

export const v2TerminalStatesWire = [
  v2TerminalEventWire,
  {
    ...v2TerminalEventWire,
    payload: {
      status: "failed",
      error: {
        code: "provider.timeout",
        message: "The model attempt timed out.",
        retryable: true,
      },
      usage: v2UsageSummaryWire,
    },
  },
  {
    ...v2TerminalEventWire,
    payload: { status: "cancelled", usage: v2UsageSummaryWire },
  },
  {
    ...v2TerminalEventWire,
    payload: {
      status: "interrupted",
      error: {
        code: "provider.timeout",
        message: "The model attempt timed out.",
        retryable: true,
      },
      usage: v2UsageSummaryWire,
    },
  },
] as const

export const v2ProgressEvents = v2ProgressEventsWire.map((event) =>
  turnSseEventSchema.parse(event),
)
export const v2TextDeltaEvent = turnSseEventSchema.parse(v2TextDeltaEventWire)
export const v2TerminalEvent = turnSseEventSchema.parse(v2TerminalEventWire)

export const v2PreferenceRecordWire = {
  preference_id: "preference_demo_001",
  source_turn_id: "turn_demo_001",
  preference: { kind: "genre", value: "Lịch sử" },
  created_at: v2Now,
  updated_at: v2CompletedAt,
} as const

export const v2PreferenceRecord = preferenceRecordSchema.parse(
  v2PreferenceRecordWire,
)

export const v2ErrorResponseWire = {
  error: {
    code: "v2.runtime_unavailable",
    message: "Không thể xử lý yêu cầu lúc này.",
    request_id: "request_v2_001",
    trace_id: "trace_v2_001",
    retryable: true,
    validation_errors: [],
  },
} as const

export function encodeV2SseEvent<Event extends { event: string; sequence: number }>(
  event: Event,
  options: { newline?: "\n" | "\r\n"; retry?: number } = {},
): string {
  const newline = options.newline ?? "\n"
  const lines = [
    `id: ${event.sequence}`,
    ...(options.retry === undefined ? [] : [`retry: ${options.retry}`]),
    `event: ${event.event}`,
    `data: ${JSON.stringify(event)}`,
  ]
  return `${lines.join(newline)}${newline}${newline}`
}
