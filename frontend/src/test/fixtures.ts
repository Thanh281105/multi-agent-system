import type {
  GatewayChatResponse,
  GatewayErrorResponse,
  GatewayStatusEvent,
} from "@/lib/contracts"

export const statusEvent: GatewayStatusEvent = {
  sequence: 1,
  phase: "request.accepted",
  message: "Gateway đã xác thực và tiếp nhận yêu cầu.",
  request_id: "req_test_123",
  trace_id: "trace_test_123",
  step_id: null,
  agent_id: null,
  status: "running",
}

export const completedResponse: GatewayChatResponse = {
  api_version: "v1",
  status: "success",
  answer: "Tai nghe Atlas phù hợp với yêu cầu [product:101].",
  session_id: "sess_test_123",
  request_id: "req_test_123",
  trace_id: "trace_test_123",
  intent: "product.search",
  active_agent: "product_agent",
  selected_product_id: 101,
  executions: [
    {
      step_id: "step_product",
      agent_id: "product_agent",
      action: "product.search",
      depends_on: [],
      status: "success",
      duration_ms: 12.4,
      error_codes: [],
    },
    {
      step_id: "step_review",
      agent_id: "review_agent",
      action: "review.summarize",
      depends_on: ["step_product"],
      status: "success",
      duration_ms: 8.2,
      error_codes: [],
    },
  ],
  provenance: [
    {
      source_type: "catalog.product",
      source_id: "product:101",
      fields: ["name", "price"],
      sample_data: true,
      observed_at: "2026-08-25T00:00:00Z",
    },
  ],
  model_calls: [
    {
      call_id: "model_call_123",
      stage: "routing",
      agent_id: "orchestrator",
      provider: "openai",
      model: "gpt-5.4-nano",
      status: "success",
      duration_ms: 130.5,
      input_tokens: 20,
      output_tokens: 8,
      total_tokens: 28,
      attempts: 1,
      fallback_used: false,
      fallback_reason: null,
      error_code: null,
    },
  ],
  warnings: [],
  sample_data: true,
  duration_ms: 244.7,
}

export const errorResponse: GatewayErrorResponse = {
  error: {
    code: "gateway.internal_error",
    message: "Không thể xử lý yêu cầu lúc này.",
    request_id: "req_test_123",
    trace_id: "trace_test_123",
    retryable: true,
    validation_errors: [],
  },
}

export function encodeTestEvent(
  event: "status" | "completed" | "error",
  data: unknown,
  options: { id?: string; retry?: number; newline?: "\n" | "\r\n" } = {},
): string {
  const newline = options.newline ?? "\n"
  const lines = [
    ...(options.id ? [`id: ${options.id}`] : []),
    ...(options.retry === undefined ? [] : [`retry: ${options.retry}`]),
    `event: ${event}`,
    `data: ${JSON.stringify(data)}`,
  ]
  return `${lines.join(newline)}${newline}${newline}`
}
