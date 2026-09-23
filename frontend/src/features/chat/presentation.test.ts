import { describe, expect, it } from "vitest"

import {
  chatReducer,
  createInitialChatState,
  type ChatState,
} from "@/features/chat/chat-state"
import {
  actionStatusLabels,
  dialogueOutcomeLabels,
  evidenceIdForDisplayLabel,
  formatArtifactLabel,
  formatEvidenceLabel,
  formatVnd,
  projectActionCard,
  projectArtifact,
  selectConversationTurnViews,
  selectDossierView,
  selectRosterItems,
  turnStatusLabels,
} from "@/features/chat/presentation"
import type {
  ActionCard,
  ConversationSummary,
  HistoryTurn,
  TurnArtifact,
  TurnResult,
  TurnSseProgressEvent,
} from "@/lib/v2-contracts"
import { completedResponse } from "@/test/fixtures"

describe("durable presentation projections", () => {
  it("keeps evidence and citations owned by their source turn", () => {
    const first = createResult("first", "[1]")
    const second = createResult("second", "[2]")
    const views = selectConversationTurnViews(
      hydratedState([
        createHistoryTurn("turn_first", "client-turn:first.123", first),
        createHistoryTurn("turn_second", "client-turn:second.123", second),
      ]),
    )

    expect(views).toHaveLength(2)
    expect(views[0].assistant?.evidence.map((item) => item.evidenceId)).toEqual([
      "evidence_first",
    ])
    expect(views[0].assistant?.citations).toEqual([
      expect.objectContaining({
        displayLabel: "[1]",
        evidenceId: "evidence_first",
      }),
    ])
    expect(views[1].assistant?.evidence.map((item) => item.evidenceId)).toEqual([
      "evidence_second",
    ])
    expect(views[1].assistant?.citations).toEqual([
      expect.objectContaining({
        displayLabel: "[2]",
        evidenceId: "evidence_second",
      }),
    ])
  })

  it("maps a citation display label only through its validated evidence link", () => {
    const result = createResult("source", "[7]")
    expect(evidenceIdForDisplayLabel(result, "[7]")).toBe("evidence_source")
    expect(evidenceIdForDisplayLabel(result, "[8]")).toBeNull()

    const mismatched = {
      ...result,
      citations: [{ ...result.citations[0], displayLabel: "[wrong]" }],
    } as TurnResult
    expect(evidenceIdForDisplayLabel(mismatched, "[wrong]")).toBeNull()
  })

  it("deduplicates an active turn already present in server history", () => {
    const liveTurn = {
      ...createHistoryTurn(
        "turn_live",
        "client-turn:live.123",
        createResult("live", "[1]"),
      ),
      status: "running",
      outcome: null,
      assistantResult: null,
      actionCards: [],
      completedAt: null,
    } as HistoryTurn
    const state = hydratedState([liveTurn])
    state.durable.activeTurn = {
      ...state.durable.activeTurn!,
      streamedAnswer: "Câu trả lời đang truyền",
    }

    const views = selectConversationTurnViews(state)
    expect(views).toHaveLength(1)
    expect(views[0]).toMatchObject({
      turnId: "turn_live",
      clientTurnId: "client-turn:live.123",
      userMessage: "Câu hỏi live",
      isActive: true,
      isStreaming: true,
    })
    expect(views[0].assistant?.text).toBe("Câu trả lời đang truyền")
  })

  it("builds the roster from actual plan capabilities and exact dependencies", () => {
    const result = createResult("route", "[1]", {
      plan: {
        planId: "plan_route",
        intent: "catalog.compare",
        revisions: [
          {
            revision: 0,
            reason: "Kế hoạch ban đầu",
            steps: [
              {
                stepId: "step_catalog",
                operationKey: "operation_catalog",
                capability: "catalog.search",
                service: "catalog_service",
                dependsOn: [],
                dataVersionIds: ["catalog_v1"],
              },
              {
                stepId: "step_reviews",
                operationKey: "operation_reviews",
                capability: "reviews.summarize",
                service: "review_service",
                dependsOn: ["step_catalog"],
                dataVersionIds: ["reviews_v1"],
              },
            ],
            addedReadStepIds: [],
            executedStepIds: ["step_catalog", "step_reviews"],
            reusedStepIds: [],
          },
        ],
      },
      executions: [
        execution("step_catalog", "catalog.search", "catalog_service"),
        execution("step_reviews", "reviews.summarize", "review_service"),
      ],
    })
    const state = hydratedState([
      createHistoryTurn("turn_route", "client-turn:route.123", result),
    ])

    const roster = selectRosterItems(state, "turn_route")
    expect(roster.map((item) => item.id)).toEqual([
      "orchestrator",
      "step_catalog",
      "step_reviews",
    ])
    expect(roster.map((item) => item.id)).not.toContain("trust_agent")

    const route = selectDossierView(state, "turn_route").route
    expect(route.map((step) => [step.stepId, step.dependsOn])).toEqual([
      ["step_catalog", []],
      ["step_reviews", ["step_catalog"]],
    ])
  })

  it("adds observed-only progress without inventing an edge", () => {
    let state = createInitialChatState({ credentialConfigured: true })
    state = chatReducer(state, {
      type: "durable.turn.started",
      generation: 1,
      conversationId: "conversation_123",
      clientTurnId: "client-turn:observed.123",
      message: "Câu hỏi",
    })
    state = chatReducer(state, {
      type: "durable.turn.event",
      generation: 1,
      event: progressEvent("step_observed"),
    })

    const dossier = selectDossierView(state)
    expect(dossier.route).toEqual([
      expect.objectContaining({
        stepId: "step_observed",
        capability: "knowledge.retrieve",
        dependsOn: [],
        status: "running",
      }),
    ])
  })

  it("projects only safe action and artifact fields", () => {
    const action = {
      ...createAction(),
      rawToolPayload: "must-not-escape",
    } as ActionCard
    const artifact = {
      ...createArtifact(),
      rawArtifactPayload: "must-not-escape",
    } as unknown as TurnArtifact

    const projectedAction = projectActionCard(action)
    const projectedArtifact = projectArtifact(artifact)
    const serialized = JSON.stringify({ projectedAction, projectedArtifact })
    expect(serialized).not.toContain("must-not-escape")
    expect(projectedAction).toMatchObject({
      actionId: "action_123",
      statusLabel: "Chờ quyết định",
      expectedResourceVersion: 2,
    })
    expect(projectedArtifact).toMatchObject({
      artifactId: "artifact_123",
      kind: "product_comparison",
    })
    expect(formatArtifactLabel(projectedArtifact)).toBe(
      "So sánh sản phẩm · So sánh sách",
    )
    expect(formatVnd(150_000)).toContain("150.000")
  })

  it("omits unavailable v2 model, usage, trace, and raw plan fields", () => {
    const result = {
      ...createResult("safe", "[1]"),
      rawPrompt: "must-not-escape",
    } as TurnResult
    const dossier = selectDossierView(
      hydratedState([
        createHistoryTurn("turn_safe", "client-turn:safe.123", result),
      ]),
      "turn_safe",
    )

    expect(dossier.modelCalls).toEqual([])
    expect(dossier.usage).toBeNull()
    expect(
      dossier.identifiers.find((entry) => entry.label === "request")?.value,
    ).toBeNull()
    expect(
      dossier.identifiers.find((entry) => entry.label === "trace")?.value,
    ).toBeNull()
    expect(JSON.stringify(dossier)).not.toContain("rawPrompt")
    expect(JSON.stringify(dossier)).not.toContain("operation_")
  })

  it("keeps status and outcome labels distinct", () => {
    expect(turnStatusLabels.completed).toBe("Hoàn tất")
    expect(dialogueOutcomeLabels.awaiting_confirmation).toBe("Chờ xác nhận")
    expect(actionStatusLabels.confirmed).toBe("Đã xác nhận")
    expect(actionStatusLabels.conflicted).toBe("Có xung đột")
  })

  it("preserves the v1 roster and dossier adapter", () => {
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      sessionId: completedResponse.session_id,
      messages: [
        { id: "user", role: "user", text: "Tìm sách" },
        { id: "assistant", role: "assistant", text: completedResponse.answer },
      ],
      request: {
        phase: "completed",
        generation: 1,
        result: completedResponse,
      },
    }

    expect(selectRosterItems(state)).toHaveLength(5)
    const dossier = selectDossierView(state)
    expect(dossier.protocol).toBe("v1")
    expect(dossier.modelCalls[0]?.model).toBe(
      completedResponse.model_calls[0].model,
    )
    expect(dossier.evidence[0]?.sourceId).toBe(
      completedResponse.provenance[0].source_id,
    )
  })

  it("formats evidence from allowlisted display fields", () => {
    const dossier = selectDossierView(
      hydratedState([
        createHistoryTurn(
          "turn_evidence",
          "client-turn:evidence.123",
          createResult("label", "[4]"),
        ),
      ]),
    )
    expect(formatEvidenceLabel(dossier.evidence[0])).toBe(
      "[4] · Nguồn label",
    )
  })
})

function hydratedState(turns: HistoryTurn[]): ChatState {
  return chatReducer(createInitialChatState({ credentialConfigured: true }), {
    type: "durable.history.hydrated",
    generation: 1,
    conversation: conversation,
    turns,
  })
}

const conversation = {
  conversationId: "conversation_123",
  mode: "shopper",
  storeId: "store_123",
  title: "Hội thoại",
  createdAt: "2026-09-13T01:00:00Z",
  updatedAt: "2026-09-13T01:00:00Z",
} as ConversationSummary

function createHistoryTurn(
  turnId: string,
  clientTurnId: string,
  result: TurnResult,
): HistoryTurn {
  const suffix = turnId.replace("turn_", "")
  return {
    turnId,
    clientTurnId,
    status: "completed",
    outcome: result.outcome,
    userMessage: "Câu hỏi " + suffix,
    assistantResult: result,
    error: null,
    actionCards: result.actionCards,
    createdAt: "2026-09-13T01:00:00Z",
    completedAt: "2026-09-13T01:01:00Z",
  }
}

function createResult(
  suffix: string,
  displayLabel: string,
  overrides: Partial<TurnResult> = {},
): TurnResult {
  const evidenceId = "evidence_" + suffix
  const claimId = "claim_" + suffix
  const citationId = "citation_" + suffix
  return {
    outcome: "answered",
    answer: "Câu trả lời " + displayLabel,
    claims: [{ claimId, text: "Khẳng định " + suffix, citationIds: [citationId] }],
    citations: [
      {
        citationId,
        claimId,
        evidenceId,
        spanId: null,
        displayLabel,
      },
    ],
    evidence: [
      {
        evidenceId,
        sourceId: "source_" + suffix,
        sourceVersionId: "source_version_" + suffix,
        chunkId: null,
        spanId: null,
        displayLabel,
        kind: "catalog",
        title: "Nguồn " + suffix,
        url: null,
        observedAt: "2026-09-13T01:00:00Z",
      },
    ],
    actionCards: [],
    artifacts: [],
    plan: null,
    executions: [],
    warnings: [],
    ...overrides,
  }
}

function createAction(): ActionCard {
  return {
    actionId: "action_123",
    proposalId: "proposal_123",
    proposalVersion: 1,
    kind: "cart_change",
    status: "proposed",
    title: "Thêm sách vào giỏ",
    requiredPermission: "cart.write",
    confirmationRequired: false,
    target: {
      resourceType: "cart",
      resourceId: "cart_123",
      expectedResourceVersion: 2,
      dataVersionIds: ["catalog_v1"],
    },
    changes: [
      {
        resourceType: "cart_item",
        resourceId: "item_123",
        field: "quantity",
        beforeInteger: 0,
        afterInteger: 1,
        beforeText: null,
        afterText: null,
      },
    ],
    expiresAt: "2026-09-13T02:00:00Z",
  }
}

function createArtifact(): TurnArtifact {
  return {
    artifactId: "artifact_123",
    resourceId: "comparison_123",
    resourceVersion: 1,
    title: "So sánh sách",
    kind: "product_comparison",
    status: "ready",
    products: [
      {
        productId: 1,
        title: "Sách một",
        author: "Tác giả một",
        priceVnd: 100_000,
        rating: 4.5,
        catalogVersionId: "catalog_v1",
      },
      {
        productId: 2,
        title: "Sách hai",
        author: null,
        priceVnd: 150_000,
        rating: null,
        catalogVersionId: "catalog_v1",
      },
    ],
    dataVersionIds: ["catalog_v1"],
  }
}

function execution(stepId: string, capability: string, service: string) {
  return {
    executionId: "execution_" + stepId,
    stepId,
    operationKey: "operation_" + stepId,
    capability,
    service,
    status: "success" as const,
    startedAt: "2026-09-13T01:00:00Z",
    completedAt: "2026-09-13T01:00:01Z",
    durationMs: 1_000,
    reused: false,
    error: null,
  }
}

function progressEvent(stepId: string): TurnSseProgressEvent {
  return {
    event: "progress",
    sequence: 1,
    requestId: "request_123",
    traceId: "trace_123",
    turnId: "turn_observed",
    phase: "step_started",
    turnStatus: "running",
    stepId,
    capability: "knowledge.retrieve",
    planRevision: 0,
    stepStatus: null,
    reused: false,
  }
}
