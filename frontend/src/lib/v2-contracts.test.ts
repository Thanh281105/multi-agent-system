import { describe, expect, it } from "vitest"

import {
  actionReadResponseSchema,
  chatRequestSchema,
  conversationDetailResponseSchema,
  conversationSummarySchema,
  historyTurnSchema,
  meResponseSchema,
  planTraceSchema,
  turnResultSchema,
  turnSseEventSchema,
  turnSseSequenceSchema,
  turnSummarySchema,
  usageSummarySchema,
  wireChatRequestSchema,
} from "@/lib/v2-contracts"
import {
  v2ActionCardWire,
  v2ArtifactsWire,
  v2CompletedTurnResponseWire,
  v2ConversationSummaryWire,
  v2HistoryTurnWire,
  v2Now,
  v2ProgressEventsWire,
  v2TerminalEventWire,
  v2TerminalStatesWire,
  v2TextDeltaEventWire,
  v2TurnResultWire,
  v2UsageSummaryWire,
} from "@/test/v2-fixtures"

describe("v2 public contracts", () => {
  it("strictly validates wire data and returns normalized app fields", () => {
    const conversation = conversationSummarySchema.parse(v2ConversationSummaryWire)
    const detail = conversationDetailResponseSchema.parse({
      conversation: v2ConversationSummaryWire,
      turns: [v2HistoryTurnWire],
    })

    expect(conversation).toMatchObject({
      conversationId: "conversation_demo_001",
      storeId: "demo",
      createdAt: v2Now,
    })
    expect(detail.turns[0].assistantResult?.actionCards[0].actionId).toBe(
      "action_checkout_001",
    )
    expect(detail).not.toHaveProperty("conversation.conversation_id")
  })

  it("rejects unknown authority and raw payload fields", () => {
    const safeRequest = {
      conversation_id: "conversation_demo_001",
      client_turn_id: "browser:turn-1",
      message: "Tìm sách lịch sử",
    }

    for (const field of ["tenant_id", "principal_id", "role", "store_id", "authority"]) {
      expect(wireChatRequestSchema.safeParse({ ...safeRequest, [field]: "attacker" }).success)
        .toBe(false)
    }
    expect(turnResultSchema.safeParse({
      ...v2TurnResultWire,
      raw_tool_payload: { secret: "must-not-cross-boundary" },
    }).success).toBe(false)
    expect(turnResultSchema.safeParse({
      ...v2TurnResultWire,
      artifacts: [{ ...v2ArtifactsWire[0], reasoning_trace: "hidden" }],
    }).success).toBe(false)
  })

  it("enforces message bounds, identity shapes, modes, and timestamps", () => {
    expect(chatRequestSchema.safeParse({
      conversation_id: "conversation_demo_001",
      client_turn_id: "browser:turn-1",
      message: "   ",
    }).success).toBe(false)
    expect(conversationSummarySchema.safeParse({
      ...v2ConversationSummaryWire,
      updated_at: "2026-09-09T07:59:00Z",
    }).success).toBe(false)
    expect(meResponseSchema.safeParse({
      principal_id: "alice",
      tenant_id: "tenant_alice",
      allowed_modes: ["shopper", "shopper"],
      store_id: "demo",
    }).success).toBe(false)
  })

  it("validates exact usage arithmetic and string-serialized Decimal costs", () => {
    expect(usageSummarySchema.parse(v2UsageSummaryWire)).toMatchObject({
      totalTokens: 30,
      estimatedCostUsd: "0.001",
      unknownReservedCostUsd: "0E-12",
    })
    expect(usageSummarySchema.safeParse({
      ...v2UsageSummaryWire,
      estimated_cost_usd: 0.001,
    }).success).toBe(false)
    expect(usageSummarySchema.safeParse({
      ...v2UsageSummaryWire,
      total_tokens: 29,
    }).success).toBe(false)
    expect(usageSummarySchema.safeParse({
      ...v2UsageSummaryWire,
      estimated_cost_usd: "0.003",
      reserved_cost_usd: "0.001",
      unknown_reserved_cost_usd: "0.001",
    }).success).toBe(true)
    expect(usageSummarySchema.safeParse({
      ...v2UsageSummaryWire,
      estimated_cost_usd: "0.0029",
      reserved_cost_usd: "0.001",
      unknown_reserved_cost_usd: "0.001",
    }).success).toBe(false)
  })

  it("enforces plan dependencies, immutable revisions, and dispatch limits", () => {
    const plan = v2TurnResultWire.plan
    expect(planTraceSchema.safeParse(plan).success).toBe(true)
    expect(planTraceSchema.safeParse({
      ...plan,
      revisions: [{
        ...plan.revisions[0],
        steps: [{ ...plan.revisions[0].steps[0], depends_on: ["step_future_001"] }],
      }],
    }).success).toBe(false)

    const step = plan.revisions[0].steps[0]
    expect(planTraceSchema.safeParse({
      ...plan,
      revisions: [
        plan.revisions[0],
        {
          revision: 1,
          reason: "mutated reuse",
          steps: [{ ...step, capability: "product.rank" }],
          added_read_step_ids: [],
          executed_step_ids: [],
          reused_step_ids: [step.step_id],
        },
      ],
    }).success).toBe(false)
  })

  it("enforces claims, citations, evidence, actions, and all artifact variants", () => {
    const result = turnResultSchema.parse(v2TurnResultWire)
    expect(result.artifacts.map((artifact) => artifact.kind)).toEqual([
      "product_comparison",
      "cart",
      "order",
      "action",
    ])
    expect(turnResultSchema.safeParse({
      ...v2TurnResultWire,
      claims: [{ ...v2TurnResultWire.claims[0], citation_ids: ["citation_unknown"] }],
    }).success).toBe(false)
    expect(turnResultSchema.safeParse({
      ...v2TurnResultWire,
      citations: [{ ...v2TurnResultWire.citations[0], display_label: "[C2]" }],
    }).success).toBe(false)
    expect(turnResultSchema.safeParse({
      ...v2TurnResultWire,
      artifacts: [{ ...v2ArtifactsWire[1], total_price_vnd: 1 }],
    }).success).toBe(false)
    expect(turnResultSchema.safeParse({
      ...v2TurnResultWire,
      artifacts: [{ ...v2ArtifactsWire[3], proposal_version: 2 }],
    }).success).toBe(false)
  })

  it("enforces terminal result, error, outcome, and history links", () => {
    expect(historyTurnSchema.safeParse(v2HistoryTurnWire).success).toBe(true)
    expect(historyTurnSchema.safeParse({
      ...v2HistoryTurnWire,
      assistant_result: null,
    }).success).toBe(false)
    expect(historyTurnSchema.safeParse({
      ...v2HistoryTurnWire,
      action_cards: [],
    }).success).toBe(false)
    expect(turnSummarySchema.safeParse({
      ...v2CompletedTurnResponseWire.turn,
      completed_at: null,
    }).success).toBe(false)
    expect(turnSummarySchema.safeParse({
      ...v2CompletedTurnResponseWire.turn,
      status: "failed",
      outcome: "answered",
    }).success).toBe(false)
  })

  it("validates action result links and excludes persistence envelopes", () => {
    expect(actionReadResponseSchema.safeParse({ action: v2ActionCardWire, result: null }).success)
      .toBe(true)
    expect(actionReadResponseSchema.safeParse({
      action: { ...v2ActionCardWire, status: "executed" },
      result: {
        action_id: v2ActionCardWire.action_id,
        status: "executed",
        resource_id: "order_demo_001",
        resource_version: 1,
        reused_result: false,
      },
    }).success).toBe(true)
    expect(actionReadResponseSchema.safeParse({
      action: { ...v2ActionCardWire, status: "executed" },
      result: {
        action_id: "action_other_001",
        status: "executed",
        resource_id: "order_demo_001",
        resource_version: 1,
        reused_result: false,
      },
    }).success).toBe(false)
    expect(actionReadResponseSchema.safeParse({
      action: v2ActionCardWire,
      result: null,
      schema_version: 1,
    }).success).toBe(false)
  })

  it("validates every progress and terminal payload variant", () => {
    for (const event of [
      ...v2ProgressEventsWire,
      v2TextDeltaEventWire,
      ...v2TerminalStatesWire,
    ]) {
      expect(turnSseEventSchema.safeParse(event).success).toBe(true)
    }
    const unsettled = turnSseEventSchema.parse({
      ...v2TerminalStatesWire[3],
      server_settled: false,
    })
    expect(unsettled).toMatchObject({
      event: "terminal",
      serverSettled: false,
    })
    const missingServerSettled: Record<string, unknown> = {
      ...v2TerminalEventWire,
    }
    delete missingServerSettled.server_settled
    expect(turnSseEventSchema.safeParse(missingServerSettled).success).toBe(false)
    expect(turnSseEventSchema.safeParse({
      ...v2ProgressEventsWire[2],
      step_id: null,
    }).success).toBe(false)
    expect(turnSseEventSchema.safeParse({
      ...v2TextDeltaEventWire,
      post_grounding: false,
    }).success).toBe(false)
    expect(turnSseEventSchema.safeParse({
      ...v2TerminalEventWire,
      payload: { status: "failed", usage: v2UsageSummaryWire },
    }).success).toBe(false)
  })

  it("requires increasing, correlated events and one final terminal", () => {
    const events = [
      ...v2ProgressEventsWire,
      v2TextDeltaEventWire,
      v2TerminalEventWire,
    ]
    expect(turnSseSequenceSchema.parse({ events }).events.at(-1)?.event).toBe("terminal")
    expect(turnSseSequenceSchema.safeParse({ events: events.slice(0, -1) }).success)
      .toBe(false)
    expect(turnSseSequenceSchema.safeParse({
      events: [events[0], { ...events[1], sequence: 1 }],
    }).success).toBe(false)
    expect(turnSseSequenceSchema.safeParse({
      events: [events[0], { ...v2TerminalEventWire, trace_id: "trace_other_001" }],
    }).success).toBe(false)
    expect(turnSseSequenceSchema.safeParse({
      events: [v2TerminalEventWire, { ...v2TerminalEventWire, sequence: 7 }],
    }).success).toBe(false)
  })
})
