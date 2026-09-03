import { z } from "zod/mini"

const nonEmptyStringSchema = z.string().check(z.minLength(1))
const nonnegativeNumberSchema = z.number().check(z.nonnegative())
const nonnegativeIntegerSchema = z.int().check(z.nonnegative())
const positiveIntegerSchema = z.int().check(z.positive())

export const taskStatusSchema = z.enum([
  "pending",
  "running",
  "success",
  "partial_success",
  "failed",
])

export const agentExecutionSchema = z.strictObject({
  step_id: nonEmptyStringSchema,
  agent_id: nonEmptyStringSchema,
  action: nonEmptyStringSchema,
  depends_on: z.array(nonEmptyStringSchema),
  status: taskStatusSchema,
  duration_ms: nonnegativeNumberSchema,
  error_codes: z.array(z.string()),
})

export const provenanceSchema = z.strictObject({
  source_type: nonEmptyStringSchema,
  source_id: nonEmptyStringSchema,
  fields: z.array(z.string()),
  sample_data: z.boolean(),
  observed_at: z.iso.datetime({ offset: true }),
})

export const modelCallSchema = z.strictObject({
  call_id: nonEmptyStringSchema,
  stage: nonEmptyStringSchema,
  agent_id: nonEmptyStringSchema,
  provider: nonEmptyStringSchema,
  model: nonEmptyStringSchema,
  response_id: z.nullable(z.string()),
  status: nonEmptyStringSchema,
  duration_ms: nonnegativeNumberSchema,
  input_tokens: nonnegativeIntegerSchema,
  cached_input_tokens: nonnegativeIntegerSchema,
  output_tokens: nonnegativeIntegerSchema,
  reasoning_tokens: nonnegativeIntegerSchema,
  total_tokens: nonnegativeIntegerSchema,
  attempts: nonnegativeIntegerSchema,
  fallback_used: z.boolean(),
  fallback_reason: z.nullable(z.string()),
  error_code: z.nullable(z.string()),
})

export const gatewayChatResponseSchema = z.strictObject({
  api_version: z.literal("v1"),
  status: taskStatusSchema,
  answer: z.string(),
  session_id: z.string().check(z.regex(/^sess_[a-zA-Z0-9_-]{3,120}$/)),
  request_id: nonEmptyStringSchema,
  trace_id: nonEmptyStringSchema,
  intent: nonEmptyStringSchema,
  active_agent: z.nullable(z.string()),
  selected_product_id: z.nullable(z.int()),
  executions: z.array(agentExecutionSchema),
  provenance: z.array(provenanceSchema),
  model_calls: z.array(modelCallSchema),
  warnings: z.array(z.string()),
  sample_data: z.boolean(),
  duration_ms: nonnegativeNumberSchema,
})

export const gatewayErrorDetailSchema = z.strictObject({
  code: nonEmptyStringSchema,
  message: nonEmptyStringSchema,
  request_id: nonEmptyStringSchema,
  trace_id: nonEmptyStringSchema,
  retryable: z.boolean(),
  validation_errors: z.array(z.record(z.string(), z.unknown())),
})

export const gatewayErrorResponseSchema = z.strictObject({
  error: gatewayErrorDetailSchema,
})

export const gatewayStatusEventSchema = z.strictObject({
  sequence: positiveIntegerSchema,
  phase: nonEmptyStringSchema,
  message: nonEmptyStringSchema,
  request_id: nonEmptyStringSchema,
  trace_id: nonEmptyStringSchema,
  step_id: z.nullable(z.string()),
  agent_id: z.nullable(z.string()),
  status: z.nullable(taskStatusSchema),
})

export const gatewayTokenEventSchema = z.strictObject({
  sequence: positiveIntegerSchema,
  delta: nonEmptyStringSchema,
  request_id: nonEmptyStringSchema,
  trace_id: nonEmptyStringSchema,
})

export type TaskStatus = z.infer<typeof taskStatusSchema>
export type AgentExecution = z.infer<typeof agentExecutionSchema>
export type Provenance = z.infer<typeof provenanceSchema>
export type ModelCall = z.infer<typeof modelCallSchema>
export type GatewayChatResponse = z.infer<typeof gatewayChatResponseSchema>
export type GatewayErrorDetail = z.infer<typeof gatewayErrorDetailSchema>
export type GatewayErrorResponse = z.infer<typeof gatewayErrorResponseSchema>
export type GatewayStatusEvent = z.infer<typeof gatewayStatusEventSchema>
export type GatewayTokenEvent = z.infer<typeof gatewayTokenEventSchema>

export type GatewayStreamEvent =
  | {
      type: "status"
      id?: string
      retry?: number
      data: GatewayStatusEvent
    }
  | {
      type: "token"
      id?: string
      retry?: number
      data: GatewayTokenEvent
    }
  | {
      type: "completed"
      id?: string
      retry?: number
      data: GatewayChatResponse
    }
  | {
      type: "error"
      id?: string
      retry?: number
      data: GatewayErrorResponse
    }
