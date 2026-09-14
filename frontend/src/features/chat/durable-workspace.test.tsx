import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { toast } from "sonner"
import { describe, expect, it, vi } from "vitest"

import {
  chatReducer,
  createInitialChatState,
  selectWorkspaceState,
} from "@/features/chat/chat-state"
import { DurableWorkspace } from "@/features/chat/durable-workspace"
import type { ChatController } from "@/features/chat/use-chat-controller"
import type { HistoryTurn } from "@/lib/v2-contracts"
import {
  v2ActionCard,
  v2ConversationSummary,
  v2HistoryTurn,
  v2PreferenceRecord,
} from "@/test/v2-fixtures"

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
    info: vi.fn(),
    success: vi.fn(),
  },
}))

describe("DurableWorkspace", () => {
  it("renders authoritative history, evidence, artifacts, and an exact action review", async () => {
    const onProvenanceRequest = vi.fn()
    const confirmAction = vi.fn(async () => ({
      action: { ...v2ActionCard, status: "executed" as const },
      result: {
        actionId: v2ActionCard.actionId,
        status: "executed" as const,
        resourceId: v2ActionCard.target.resourceId,
        resourceVersion: 4,
        reusedResult: false,
      },
    }))
    const controller = createDurableController({ confirmAction })
    render(
      <DurableWorkspace
        controller={controller}
        onCredentialRequest={vi.fn()}
        onProvenanceRequest={onProvenanceRequest}
        selectedTurnId={v2HistoryTurn.turnId}
        onTurnSelect={vi.fn()}
      />,
    )

    expect(screen.getByText(v2HistoryTurn.userMessage!)).toBeInTheDocument()
    expect(screen.getByText(/Đã chuẩn bị đề xuất có kiểm chứng/)).toBeInTheDocument()
    expect(screen.getByText(/So sánh sản phẩm/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Mở nguồn [C1]" }))
    expect(onProvenanceRequest).toHaveBeenCalledWith("evidence_demo_001")

    fireEvent.click(screen.getByRole("button", { name: "Xem đề xuất" }))
    const dialog = await screen.findByRole("dialog", {
      name: v2ActionCard.title,
    })
    expect(dialog).toHaveTextContent("ecommerce.write")
    expect(within(dialog).getByText("Proposal version")).toBeInTheDocument()
    expect(dialog).toHaveTextContent("Resource version")
    expect(dialog).toHaveTextContent("cart_demo_001")
    fireEvent.click(within(dialog).getByRole("button", { name: "Xác nhận" }))
    await waitFor(() => expect(confirmAction).toHaveBeenCalledWith(v2ActionCard.actionId, 1))
    expect(document.body).not.toHaveTextContent("rawToolPayload")
  })

  it("changes mode through the controller and saves memory only with a completed source turn", async () => {
    const changeMode = vi.fn(async () => "completed" as const)
    const putPreference = vi.fn(async () => v2PreferenceRecord)
    const controller = createDurableController({
      changeMode,
      putPreference,
      allowedModes: ["shopper", "merchant"],
    })
    render(
      <DurableWorkspace
        controller={controller}
        onCredentialRequest={vi.fn()}
        onProvenanceRequest={vi.fn()}
        onTurnSelect={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByLabelText("Mode"), {
      target: { value: "merchant" },
    })
    expect(changeMode).toHaveBeenCalledWith("merchant")

    fireEvent.click(screen.getByText("Memory minh bạch"))
    fireEvent.change(screen.getByLabelText("Turn nguồn"), {
      target: { value: v2HistoryTurn.turnId },
    })
    fireEvent.change(screen.getByLabelText("Giá trị"), {
      target: { value: "Lịch sử" },
    })
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Lưu" }))
    })
    expect(putPreference).toHaveBeenCalledWith(v2HistoryTurn.turnId, {
      kind: "genre",
      value: "Lịch sử",
    })
  })

  it("does not report reject success while readback is still proposed", async () => {
    vi.mocked(toast.error).mockClear()
    vi.mocked(toast.success).mockClear()
    const rejectAction = vi.fn(async () => ({ action: v2ActionCard, result: null }))
    const controller = createDurableController({ rejectAction })
    render(
      <DurableWorkspace
        controller={controller}
        onCredentialRequest={vi.fn()}
        onProvenanceRequest={vi.fn()}
        onTurnSelect={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Xem đề xuất" }))
    const dialog = await screen.findByRole("dialog", { name: v2ActionCard.title })
    fireEvent.click(within(dialog).getByRole("button", { name: "Từ chối" }))
    await waitFor(() => {
      expect(rejectAction).toHaveBeenCalledWith(v2ActionCard.actionId, 1)
    })
    expect(toast.success).not.toHaveBeenCalled()
    expect(toast.error).toHaveBeenCalledWith(
      "Chưa thể xác nhận đề xuất đã được từ chối. Trạng thái hiện tại được giữ lại.",
    )
    expect(within(dialog).getByRole("button", { name: "Từ chối" })).toBeEnabled()
  })

  it("renders cancelled, interrupted, expired, and conflicted states explicitly", () => {
    const cancelledTurn: HistoryTurn = {
      ...v2HistoryTurn,
      turnId: "turn_cancelled_001",
      clientTurnId: "browser:cancelled",
      status: "cancelled",
      outcome: null,
      assistantResult: null,
      error: null,
      actionCards: [],
    }
    const interruptedTurn: HistoryTurn = {
      ...cancelledTurn,
      turnId: "turn_interrupted_001",
      clientTurnId: "browser:interrupted",
      status: "interrupted",
      error: {
        code: "stream.interrupted",
        message: "Luồng kết quả bị gián đoạn. Vui lòng thử lại.",
        retryable: true,
      },
    }
    const expiredAction = {
      ...v2ActionCard,
      actionId: "action_expired_001",
      proposalId: "proposal_expired_001",
      status: "expired" as const,
    }
    const conflictedAction = {
      ...v2ActionCard,
      actionId: "action_conflicted_001",
      proposalId: "proposal_conflicted_001",
      status: "conflicted" as const,
    }
    const completedTurn: HistoryTurn = {
      ...v2HistoryTurn,
      turnId: "turn_actions_001",
      clientTurnId: "browser:actions",
      assistantResult: {
        ...v2HistoryTurn.assistantResult!,
        actionCards: [expiredAction, conflictedAction],
      },
      actionCards: [expiredAction, conflictedAction],
    }
    const state = chatReducer(createInitialChatState({ credentialConfigured: true }), {
      type: "durable.history.hydrated",
      generation: 1,
      conversation: v2ConversationSummary,
      turns: [cancelledTurn, interruptedTurn, completedTurn],
    })
    render(
      <DurableWorkspace
        controller={createDurableController({}, state)}
        onCredentialRequest={vi.fn()}
        onProvenanceRequest={vi.fn()}
        onTurnSelect={vi.fn()}
      />,
    )

    expect(screen.getByText("Đã dừng")).toBeInTheDocument()
    expect(screen.getByText("Bị gián đoạn")).toBeInTheDocument()
    expect(screen.getByText(/Đã hết hạn/)).toBeInTheDocument()
    expect(screen.getByText(/Có xung đột/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Thử nối lại" })).toBeInTheDocument()
  })
})

function createDurableController(
  overrides: Partial<NonNullable<ChatController["durable"]>> = {},
  stateOverride?: ChatController["state"],
): ChatController & { durable: NonNullable<ChatController["durable"]> } {
  const state = stateOverride ??
    chatReducer(createInitialChatState({ credentialConfigured: true }), {
      type: "durable.history.hydrated",
      generation: 1,
      conversation: v2ConversationSummary,
      turns: [v2HistoryTurn],
    })
  const durable: NonNullable<ChatController["durable"]> = {
    identity: {
      principalId: "principal@example.test",
      tenantId: "tenant_demo",
      allowedModes: ["shopper"],
      storeId: "demo",
    },
    allowedModes: ["shopper"],
    selectedMode: "shopper",
    conversations: [v2ConversationSummary],
    phase: "ready",
    failure: null,
    bootstrap: vi.fn(async () => "completed" as const),
    listConversations: vi.fn(async () => [v2ConversationSummary]),
    createConversation: vi.fn(async () => v2ConversationSummary),
    switchConversation: vi.fn(async () => "completed" as const),
    deleteConversation: vi.fn(async () => "completed" as const),
    changeMode: vi.fn(async () => "completed" as const),
    sendMessage: vi.fn(async () => "completed" as const),
    retryPendingTurn: vi.fn(async () => "completed" as const),
    cancelRequest: vi.fn(async () => null),
    getAction: vi.fn(async () => ({ action: v2ActionCard, result: null })),
    confirmAction: vi.fn(async () => ({ action: v2ActionCard, result: null })),
    rejectAction: vi.fn(async () => ({ action: v2ActionCard, result: null })),
    listPreferences: vi.fn(async () => []),
    putPreference: vi.fn(async () => v2PreferenceRecord),
    deletePreference: vi.fn(async () => "completed" as const),
    ...overrides,
  }
  return {
    state,
    workspaceState: selectWorkspaceState(state),
    storageAvailable: true,
    configureCredential: vi.fn(() => true),
    clearCredential: vi.fn(),
    sendMessage: vi.fn(async () => "completed" as const),
    cancelRequest: vi.fn(),
    resetSession: vi.fn(),
    durable,
  }
}
