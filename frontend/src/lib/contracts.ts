import { z } from "zod"

export const taskStatusSchema = z.enum([
  "pending",
  "running",
  "success",
  "partial_success",
  "failed",
])

export const agentExecutionSchema = z
  .object({
    step_id: z.string().min(1),
    agent_id: z.string().min(1),
    action: z.string().min(1),
    depends_on: z.array(z.string().min(1)),
    status: taskStatusSchema,
    duration_ms: z.number().nonnegative().finite(),
    error_codes: z.array(z.string()),
  })
  .strict()

export const provenanceSchema = z
  .object({
    source_type: z.string().min(1),
    source_id: z.string().min(1),
    fields: z.array(z.string()),
    sample_data: z.boolean(),
    observed_at: z.iso.datetime({ offset: true }),
  })
  .strict()

export const modelCallSchema = z
  .object({
    call_id: z.string().min(1),
    stage: z.string().min(1),
    agent_id: z.string().min(1),
    provider: z.string().min(1),
    model: z.string().min(1),
    status: z.string().min(1),
    duration_ms: z.number().nonnegative().finite(),
    input_tokens: z.number().int().nonnegative(),
    output_tokens: z.number().int().nonnegative(),
    total_tokens: z.number().int().nonnegative(),
    attempts: z.number().int().nonnegative(),
    fallback_used: z.boolean(),
    fallback_reason: z.string().nullable(),
    error_code: z.string().nullable(),
  })
  .strict()

export const gatewayChatResponseSchema = z
  .object({
    api_version: z.literal("v1"),
    status: taskStatusSchema,
    answer: z.string(),
    session_id: z.string().regex(/^sess_[a-zA-Z0-9_-]{3,120}$/),
    request_id: z.string().min(1),
    trace_id: z.string().min(1),
    intent: z.string().min(1),
    active_agent: z.string().nullable(),
    selected_product_id: z.number().int().nullable(),
    executions: z.array(agentExecutionSchema),
    provenance: z.array(provenanceSchema),
    model_calls: z.array(modelCallSchema),
    warnings: z.array(z.string()),
    sample_data: z.boolean(),
    duration_ms: z.number().nonnegative().finite(),
  })
  .strict()

export const gatewayErrorDetailSchema = z
  .object({
    code: z.string().min(1),
    message: z.string().min(1),
    request_id: z.string().min(1),
    trace_id: z.string().min(1),
    retryable: z.boolean(),
    validation_errors: z.array(z.record(z.string(), z.unknown())),
  })
  .strict()

export const gatewayErrorResponseSchema = z
  .object({
    error: gatewayErrorDetailSchema,
  })
  .strict()

export const gatewayStatusEventSchema = z
  .object({
    sequence: z.number().int().positive(),
    phase: z.string().min(1),
    message: z.string().min(1),
    request_id: z.string().min(1),
    trace_id: z.string().min(1),
    step_id: z.string().nullable(),
    agent_id: z.string().nullable(),
    status: taskStatusSchema.nullable(),
  })
  .strict()

export type TaskStatus = z.infer<typeof taskStatusSchema>
export type AgentExecution = z.infer<typeof agentExecutionSchema>
export type Provenance = z.infer<typeof provenanceSchema>
export type ModelCall = z.infer<typeof modelCallSchema>
export type GatewayChatResponse = z.infer<typeof gatewayChatResponseSchema>
export type GatewayErrorDetail = z.infer<typeof gatewayErrorDetailSchema>
export type GatewayErrorResponse = z.infer<typeof gatewayErrorResponseSchema>
export type GatewayStatusEvent = z.infer<typeof gatewayStatusEventSchema>

export type GatewayStreamEvent =
  | {
      type: "status"
      id?: string
      retry?: number
      data: GatewayStatusEvent
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
