import axe from "axe-core"
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
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

const EVIDENCE_ATLAS_TEST_TIMEOUT_MS = 20_000
const XL_MEDIA_QUERY = "(min-width: 1280px)"

describe("EvidenceAtlas", { timeout: EVIDENCE_ATLAS_TEST_TIMEOUT_MS }, () => {
  it("renders the evidence-first initial workspace with no axe violations", async () => {
    const controller = createController()
    const { container } = render(<EvidenceAtlas controller={controller} />)

    expect(
      screen.getByRole("heading", {
        name: "Hỏi về sách. Kiểm tra từng nguồn.",
      }),
    ).toBeVisible()
    expect(screen.getByRole("note")).toHaveTextContent(
      "không phản ánh danh mục, giá hay mức quan tâm hiện tại trên Tiki",
    )
    expect(screen.getByText("Tiki Books · Historical snapshot")).toBeVisible()
    expect(screen.getByText("Danh mục sách")).toBeVisible()
    expect(
      screen.getByText(/Hỏi về sách trong dữ liệu Tiki Books lịch sử/),
    ).toBeVisible()
    const suggestion = screen.getByRole("button", {
      name: /Tìm sách học tiếng Anh/,
    })
    expect(suggestion).toBeVisible()
    const composer = screen.getByLabelText("Câu hỏi về sách")
    expect(composer).toHaveAttribute(
      "placeholder",
      "Ví dụ: Tìm sách học tiếng Anh dưới 150.000đ, có đánh giá tích cực…",
    )
    expect(composer).toBeEnabled()
    fireEvent.click(suggestion)
    expect(composer).toHaveValue(
      "Tìm sách học tiếng Anh dưới 150.000đ, có đánh giá tích cực và ít phản hồi tiêu cực.",
    )
    expect(composer).toHaveFocus()
    expect(
      screen.getByText("Chỉ chạy hành động được phép"),
    ).toBeInTheDocument()
    expect(screen.queryByText("Thương Trí")).not.toBeInTheDocument()
    expect(screen.queryByText("Xu hướng sách")).not.toBeInTheDocument()

    const results = await axe.run(container, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
      rules: { "color-contrast": { enabled: false } },
    })
    expect(results.violations).toEqual([])
  })

  it("opens an accessible memory-only Gateway credential dialog", async () => {
    const configureCredential = vi
      .fn<(value: string) => boolean>()
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true)
    render(
      <EvidenceAtlas controller={createController({ configureCredential })} />,
    )

    const workspaceControls = screen.getByRole("navigation", {
      name: "Điều khiển workspace",
    })
    fireEvent.click(
      within(workspaceControls).getByRole("button", {
        name: "Thiết lập Gateway API key",
      }),
    )
    const dialog = await screen.findByRole("dialog", {
      name: "Thiết lập Gateway API key",
    })
    expect(dialog).toHaveTextContent(
      "Đây không phải API key của nhà cung cấp model",
    )
    const input = screen.getByLabelText("Gateway API key")
    expect(input).toHaveAttribute("aria-describedby", "gateway-key-description")
    fireEvent.change(input, { target: { value: "short" } })
    fireEvent.submit(input.closest("form")!)
    expect(screen.getByRole("alert")).toHaveTextContent("ít nhất 8 ký tự")
    expect(input).toHaveAttribute(
      "aria-describedby",
      "gateway-key-description gateway-key-error",
    )

    fireEvent.change(input, { target: { value: "gateway-secret" } })
    fireEvent.submit(input.closest("form")!)
    expect(configureCredential).toHaveBeenLastCalledWith("gateway-secret")
    expect(
      screen.queryByRole("dialog", { name: "Thiết lập Gateway API key" }),
    ).not.toBeInTheDocument()
  })

  it("restores focus after the credential dialog closes", async () => {
    render(<EvidenceAtlas controller={createController()} />)
    const trigger = within(
      screen.getByRole("navigation", { name: "Điều khiển workspace" }),
    ).getByRole("button", { name: "Thiết lập Gateway API key" })
    fireEvent.click(trigger)
    expect(
      await screen.findByRole("dialog", { name: "Thiết lập Gateway API key" }),
    ).toBeInTheDocument()

    fireEvent.keyDown(document, { key: "Escape" })

    await waitFor(() => expect(trigger).toHaveFocus())
    expect(
      screen.queryByRole("dialog", { name: "Thiết lập Gateway API key" }),
    ).not.toBeInTheDocument()
  })

  it("does not render desktop rail trees on a mobile viewport", async () => {
    const listeners = new Set<(event: MediaQueryListEvent) => void>()
    let matches = false
    const mediaQuery = {
      get matches() {
        return matches
      },
      media: XL_MEDIA_QUERY,
      onchange: null,
      addEventListener: (
        type: string,
        listener: EventListenerOrEventListenerObject,
      ) => {
        if (type === "change" && typeof listener === "function") {
          listeners.add(listener as (event: MediaQueryListEvent) => void)
        }
      },
      removeEventListener: (
        type: string,
        listener: EventListenerOrEventListenerObject,
      ) => {
        if (type === "change" && typeof listener === "function") {
          listeners.delete(listener as (event: MediaQueryListEvent) => void)
        }
      },
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(() => true),
    } satisfies MediaQueryList
    vi.stubGlobal("matchMedia", vi.fn(() => mediaQuery))

    try {
      render(<EvidenceAtlas controller={createController()} />)

      expect(
        document.querySelector('aside[aria-label="Danh sách agent"]'),
      ).not.toBeInTheDocument()
      expect(
        document.querySelector('aside[aria-label="Chi tiết thực thi"]'),
      ).not.toBeInTheDocument()

      fireEvent.click(
        screen.getByRole("button", { name: "Mở danh sách agent" }),
      )
      expect(
        await screen.findByRole("dialog", { name: "Các agent" }),
      ).toBeInTheDocument()

      matches = true
      act(() => {
        for (const listener of listeners) {
          listener({ matches: true } as MediaQueryListEvent)
        }
      })
      expect(
        document.querySelector('aside[aria-label="Danh sách agent"]'),
      ).toBeInTheDocument()
      expect(
        document.querySelector('aside[aria-label="Chi tiết thực thi"]'),
      ).toBeInTheDocument()
      await waitFor(() => {
        expect(
          screen.queryByRole("dialog", { name: "Các agent" }),
        ).not.toBeInTheDocument()
        expect(document.getElementById("workspace")).toHaveFocus()
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it("renders truthful DAG, model ledger, provenance, and trace metadata", () => {
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      sessionId: completedResponse.session_id,
      messages: [
        { id: "user", role: "user", text: "Tìm sách chiêm tinh" },
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
    expect(screen.getByText("phụ thuộc · step_product")).toBeInTheDocument()
    expect(screen.getByText(/Nhật Ký Tarot phù hợp/)).toBeInTheDocument()
    expect(screen.getByText(/Câu trả lời có nguồn/)).toBeInTheDocument()
    expect(screen.queryByText(/agent_results/i)).not.toBeInTheDocument()
  })

  it("shows live progress and exposes a working cancel control", () => {
    const cancelRequest = vi.fn()
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      messages: [
        { id: "user", role: "user", text: "Tìm sách học tiếng Anh" },
      ],
      request: {
        phase: "streaming",
        generation: 2,
        statuses: [statusEvent],
        answer: "Nhật Ký Tarot ",
        lastSequence: 1,
      },
    }
    render(
      <EvidenceAtlas controller={createController({ state, cancelRequest })} />,
    )

    expect(screen.getByText("Đã tiếp nhận")).toBeInTheDocument()
    expect(screen.getByText(/Nhật Ký Tarot/)).toBeInTheDocument()
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
        { id: "user", role: "user", text: "Phân tích sách" },
        { id: "assistant", role: "assistant", text: failedResponse.answer },
      ],
    }
    render(<EvidenceAtlas controller={createController({ state })} />)

    expect(
      screen
        .getByText("Yêu cầu chưa hoàn tất")
        .closest('[role="status"]'),
    ).toHaveTextContent("Planner không hoàn tất")
    const dossier = screen.getByRole("region", { name: "Chi tiết thực thi" })
    const dossierHeader = within(dossier)
      .getByRole("heading", { name: "Chi tiết thực thi" })
      .closest("header")!
    expect(within(dossierHeader).getByText("Thất bại")).toBeInTheDocument()
    expect(screen.getByText("Fallback theo quy tắc")).toBeInTheDocument()
    expect(within(dossierHeader).queryByText("Hoàn tất")).not.toBeInTheDocument()
  })

  it("keeps cancelled conversation and dossier states consistent", () => {
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      request: { phase: "cancelled", generation: 1 },
    }
    render(<EvidenceAtlas controller={createController({ state })} />)

    expect(screen.getByText("Đã dừng theo yêu cầu")).toBeInTheDocument()
    const dossier = screen.getByRole("region", { name: "Chi tiết thực thi" })
    expect(within(dossier).getByText("Đã dừng")).toBeInTheDocument()
    expect(within(dossier).queryByText("Chưa chạy")).not.toBeInTheDocument()
  })

  it("opens and focuses a unique mobile provenance target from a claim citation", async () => {
    const state: ChatState = {
      ...createInitialChatState({ credentialConfigured: true }),
      sessionId: completedResponse.session_id,
      messages: [
        { id: "user", role: "user", text: "Tìm sách chiêm tinh" },
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
      await screen.findByRole("dialog", { name: "Chi tiết thực thi" }),
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
    const composer = screen.getByLabelText("Câu hỏi về sách")
    composer.focus()

    const streamingState: ChatState = {
      ...idleState,
      messages: [
        { id: "user", role: "user", text: "Tìm sách học tiếng Anh" },
      ],
      request: {
        phase: "streaming",
        generation: 1,
        statuses: [statusEvent],
        answer: "",
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
      expect(screen.getByLabelText("Câu hỏi về sách")).toHaveFocus(),
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
