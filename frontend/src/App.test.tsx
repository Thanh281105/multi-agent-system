import axe from "axe-core"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { EvidenceAtlas } from "@/App"
import {
  createInitialChatState,
  selectWorkspaceState,
  type ChatState,
} from "@/features/chat/chat-state"
import { provenanceAnchorId } from "@/features/chat/presentation"
import type { ChatController } from "@/features/chat/use-chat-controller"
import { completedResponse, statusEvent } from "@/test/fixtures"

describe("EvidenceAtlas", () => {
  it("renders the evidence-first initial workspace with no axe violations", async () => {
    const controller = createController()
    const { container } = render(<EvidenceAtlas controller={controller} />)

    expect(
      screen.getByRole("heading", {
        name: "Mỗi kết luận đều có một đường về nguồn.",
      }),
    ).toBeVisible()
    expect(screen.getByRole("note")).toHaveTextContent(
      "không đại diện toàn bộ thị trường",
    )
    expect(screen.getByLabelText("Câu hỏi cần điều phối")).toBeEnabled()
    expect(screen.getByText("Action allowlist bật")).toBeInTheDocument()

    const results = await axe.run(container, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
      rules: { "color-contrast": { enabled: false } },
    })
    expect(results.violations).toEqual([])
  })

  it("opens an accessible memory-only Gateway credential dialog", () => {
    const configureCredential = vi
      .fn<(value: string) => boolean>()
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true)
    render(
      <EvidenceAtlas controller={createController({ configureCredential })} />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Kết nối Gateway key" }))
    const dialog = screen.getByRole("dialog", {
      name: "Kết nối với Agent Gateway",
    })
    expect(dialog).toHaveTextContent("Đây không phải OpenAI key phía backend")
    const input = screen.getByLabelText("Gateway API key")
    fireEvent.change(input, { target: { value: "short" } })
    fireEvent.submit(input.closest("form")!)
    expect(screen.getByRole("alert")).toHaveTextContent("ít nhất 8 ký tự")

    fireEvent.change(input, { target: { value: "gateway-secret" } })
    fireEvent.submit(input.closest("form")!)
    expect(configureCredential).toHaveBeenLastCalledWith("gateway-secret")
    expect(
      screen.queryByRole("dialog", { name: "Kết nối với Agent Gateway" }),
    ).not.toBeInTheDocument()
  })

  it("renders truthful DAG, model ledger, provenance, and trace metadata", () => {
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      sessionId: completedResponse.session_id,
      messages: [
        { id: "user", role: "user", text: "Tìm tai nghe" },
        {
          id: "assistant",
          role: "assistant",
          text: completedResponse.answer,
        },
      ],
      request: {
        phase: "completed",
        generation: 1,
        result: completedResponse,
      },
    }
    render(<EvidenceAtlas controller={createController({ state })} />)

    expect(
      screen.getByRole("img", {
        name: "Đồ thị phụ thuộc giữa các bước thực thi",
      }),
    ).toBeInTheDocument()
    expect(screen.getByText("gpt-5.4-nano")).toBeInTheDocument()
    expect(screen.getByText("product:101")).toBeInTheDocument()
    expect(screen.getByText("từ · step_product")).toBeInTheDocument()
    expect(screen.getByText(/Kết luận có nguồn/)).toBeInTheDocument()
    expect(screen.queryByText(/agent_results/i)).not.toBeInTheDocument()
  })

  it("shows live progress and exposes a working cancel control", () => {
    const cancelRequest = vi.fn()
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      messages: [{ id: "user", role: "user", text: "Tìm laptop" }],
      request: {
        phase: "streaming",
        generation: 2,
        statuses: [statusEvent],
        lastSequence: 1,
      },
    }
    render(
      <EvidenceAtlas controller={createController({ state, cancelRequest })} />,
    )

    expect(screen.getByText("Đã tiếp nhận")).toBeInTheDocument()
    fireEvent.click(
      screen.getByRole("button", { name: "Dừng yêu cầu đang chạy" }),
    )
    expect(cancelRequest).toHaveBeenCalledOnce()
  })

  it("keeps completed failure and fallback states truthful", () => {
    const failedResponse = {
      ...completedResponse,
      status: "failed" as const,
      warnings: ["Planner không hoàn tất trong giới hạn thời gian."],
      model_calls: [
        {
          ...completedResponse.model_calls[0],
          status: "failed",
          fallback_used: true,
          fallback_reason: "Structured output không hợp lệ.",
          error_code: "model.invalid_output",
        },
      ],
    }
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      request: {
        phase: "completed",
        generation: 1,
        result: failedResponse,
      },
      messages: [
        { id: "user", role: "user", text: "Phân tích sản phẩm" },
        { id: "assistant", role: "assistant", text: failedResponse.answer },
      ],
    }
    render(<EvidenceAtlas controller={createController({ state })} />)

    expect(
      screen
        .getByText("Tuyến chưa đạt trạng thái hoàn tất")
        .closest('[role="status"]'),
    ).toHaveTextContent("Planner không hoàn tất")
    expect(screen.getByText("Thất bại")).toBeInTheDocument()
    expect(screen.getByText("Fallback an toàn")).toBeInTheDocument()
    expect(screen.queryByText("Đã kiểm chứng")).not.toBeInTheDocument()
  })

  it("opens and focuses a unique mobile provenance target from a claim citation", async () => {
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      sessionId: completedResponse.session_id,
      messages: [
        { id: "user", role: "user", text: "Tìm tai nghe" },
        { id: "assistant", role: "assistant", text: completedResponse.answer },
      ],
      request: {
        phase: "completed",
        generation: 1,
        result: completedResponse,
      },
    }
    render(<EvidenceAtlas controller={createController({ state })} />)

    fireEvent.click(
      screen.getByRole("button", { name: "Mở nguồn product:101" }),
    )
    expect(
      await screen.findByRole("dialog", { name: "Hồ sơ thực thi" }),
    ).toBeInTheDocument()

    const target = document.getElementById(
      provenanceAnchorId("mobile", "product:101"),
    )
    expect(target).not.toBeNull()
    await waitFor(() => expect(target).toHaveFocus())

    const ids = Array.from(document.querySelectorAll("[id]"), (node) => node.id)
    expect(new Set(ids).size).toBe(ids.length)

    const results = await axe.run(document.body, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
      rules: { "color-contrast": { enabled: false } },
    })
    expect(results.violations).toEqual([])
  })

  it("moves focus to cancel during streaming and restores the composer", async () => {
    const idleState = createInitialChatState({ credentialConfigured: true })
    const { rerender } = render(
      <EvidenceAtlas controller={createController({ state: idleState })} />,
    )
    const composer = screen.getByLabelText("Câu hỏi cần điều phối")
    composer.focus()

    const streamingState: ChatState = {
      ...idleState,
      messages: [{ id: "user", role: "user", text: "Tìm laptop" }],
      request: {
        phase: "streaming",
        generation: 1,
        statuses: [statusEvent],
        lastSequence: 1,
      },
    }
    rerender(
      <EvidenceAtlas
        controller={createController({ state: streamingState })}
      />,
    )
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Dừng yêu cầu đang chạy" }),
      ).toHaveFocus(),
    )

    const completedState: ChatState = {
      ...idleState,
      messages: [
        ...streamingState.messages,
        { id: "assistant", role: "assistant", text: completedResponse.answer },
      ],
      request: {
        phase: "completed",
        generation: 1,
        result: completedResponse,
      },
    }
    rerender(
      <EvidenceAtlas
        controller={createController({ state: completedState })}
      />,
    )
    await waitFor(() =>
      expect(screen.getByLabelText("Câu hỏi cần điều phối")).toHaveFocus(),
    )
  })

  it("preserves multiline answers and wraps unbroken identifiers", () => {
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      messages: [
        {
          id: "assistant",
          role: "assistant",
          text: `Dòng thứ nhất\n${"identifier".repeat(80)}`,
        },
      ],
      request: { phase: "idle", generation: 0 },
    }
    render(<EvidenceAtlas controller={createController({ state })} />)

    const bubble = screen
      .getByText(/Dòng thứ nhất/)
      .closest('[data-slot="bubble-content"]')
    expect(bubble).toHaveClass("whitespace-pre-wrap")
    expect(bubble).toHaveClass("[overflow-wrap:anywhere]")
  })
})

function createController(
  overrides: Partial<ChatController> = {},
): ChatController {
  const state = overrides.state ?? createInitialChatState()
  return {
    state,
    workspaceState: selectWorkspaceState(state),
    storageAvailable: true,
    configureCredential: vi.fn(() => true),
    clearCredential: vi.fn(),
    sendMessage: vi.fn(async () => "completed" as const),
    cancelRequest: vi.fn(),
    resetSession: vi.fn(),
    ...overrides,
  }
}
