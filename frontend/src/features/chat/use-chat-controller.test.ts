import { act, renderHook, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { useChatController } from "@/features/chat/use-chat-controller"
import { GatewayClientError } from "@/lib/gateway-stream"
import { completedResponse, statusEvent, tokenEvent } from "@/test/fixtures"

describe("useChatController", () => {
  it("keeps credentials in memory while persisting only session and history", async () => {
    const storage = createStorage()
    const send = vi.fn(async (request) => {
      request.onStatus?.(statusEvent)
      request.onToken?.(tokenEvent)
      return completedResponse
    })
    const ids = createIds()
    const { result } = renderHook(() =>
      useChatController({ storage, send, createId: ids }),
    )

    act(() => {
      expect(result.current.configureCredential("  gateway-secret  ")).toBe(true)
    })
    let outcome: Awaited<ReturnType<typeof result.current.sendMessage>> | undefined
    await act(async () => {
      outcome = await result.current.sendMessage("  Tìm sách chiêm tinh  ")
    })

    expect(outcome).toBe("completed")
    expect(send).toHaveBeenCalledWith(
      expect.objectContaining({
        apiKey: "gateway-secret",
        message: "Tìm sách chiêm tinh",
        sessionId: null,
        onToken: expect.any(Function),
      }),
    )
    expect(result.current.state.request.phase).toBe("completed")
    expect(result.current.state.messages).toHaveLength(2)

    await waitFor(() => {
      expect(storage.dump()["thuong-tri.session-id"]).toBe("sess_test_123")
    })
    const persisted = JSON.stringify(storage.dump())
    expect(persisted).not.toContain("gateway-secret")
    expect(Object.keys(storage.dump())).toEqual([
      "thuong-tri.history",
      "thuong-tri.session-id",
    ])
  })

  it("cancels by advancing generation and ignores the late result", async () => {
    let resolveRequest: ((value: typeof completedResponse) => void) | undefined
    const send = vi.fn(
      () =>
        new Promise<typeof completedResponse>((resolve) => {
          resolveRequest = resolve
        }),
    )
    const { result } = renderHook(() =>
      useChatController({
        storage: createStorage(),
        send,
        createId: createIds(),
      }),
    )
    act(() => {
      result.current.configureCredential("gateway-secret")
    })

    let request: Promise<string>
    act(() => {
      request = result.current.sendMessage("Tìm sách học tiếng Anh")
    })
    await waitFor(() => {
      expect(result.current.state.request.phase).toBe("streaming")
    })
    act(() => result.current.cancelRequest())
    expect(result.current.state.request).toMatchObject({
      phase: "cancelled",
      generation: 2,
    })

    await act(async () => {
      resolveRequest?.(completedResponse)
      expect(await request).toBe("cancelled")
    })
    expect(result.current.state.request.phase).toBe("cancelled")
  })

  it("clears an invalid credential after an authentication failure", async () => {
    const send = vi.fn().mockRejectedValue(
      new GatewayClientError(
        "gateway.authentication_failed",
        "API key không hợp lệ.",
        { source: "gateway", requestId: "req_auth", traceId: "trace_auth" },
      ),
    )
    const { result } = renderHook(() =>
      useChatController({ storage: createStorage(), send }),
    )
    act(() => {
      result.current.configureCredential("invalid-key")
    })

    await act(async () => {
      expect(await result.current.sendMessage("Tìm sách học tiếng Anh")).toBe(
        "failed",
      )
    })

    expect(result.current.state.credentialConfigured).toBe(false)
    expect(result.current.state.request).toMatchObject({
      phase: "failed",
      failure: {
        source: "gateway",
        code: "gateway.authentication_failed",
        requestId: "req_auth",
      },
    })
    await expect(result.current.sendMessage("Thử lại")).resolves.toBe(
      "credential_required",
    )
  })

  it("reports blocked preconditions without starting a request", async () => {
    const { result } = renderHook(() =>
      useChatController({ storage: null, online: false }),
    )

    await expect(
      result.current.sendMessage("Tìm sách học tiếng Anh"),
    ).resolves.toBe("credential_required")
    act(() => {
      expect(result.current.configureCredential("short")).toBe(false)
      expect(result.current.configureCredential("gateway-secret")).toBe(true)
    })
    await expect(
      result.current.sendMessage("Tìm sách học tiếng Anh"),
    ).resolves.toBe("offline")
    expect(result.current.state.request.phase).toBe("idle")
  })

  it("resets session history without clearing the in-memory credential", async () => {
    const storage = createStorage({
      "thuong-tri.session-id": "sess_existing_123",
      "thuong-tri.history": JSON.stringify([
        { id: "old", role: "user", text: "Câu hỏi cũ" },
      ]),
    })
    const { result } = renderHook(() =>
      useChatController({ storage, createId: createIds() }),
    )
    act(() => {
      result.current.configureCredential("gateway-secret")
      result.current.resetSession()
    })

    expect(result.current.state).toMatchObject({
      sessionId: null,
      messages: [],
      credentialConfigured: true,
    })
    expect(storage.dump()["thuong-tri.session-id"]).toBeUndefined()

    act(() => result.current.clearCredential())
    expect(result.current.state.credentialConfigured).toBe(false)
  })

  it("surfaces unavailable session storage without crashing", async () => {
    const blockedStorage = {
      getItem: vi.fn(() => {
        throw new Error("blocked")
      }),
      setItem: vi.fn(() => {
        throw new Error("blocked")
      }),
      removeItem: vi.fn(() => {
        throw new Error("blocked")
      }),
    }
    const { result } = renderHook(() =>
      useChatController({ storage: blockedStorage }),
    )

    await waitFor(() => expect(result.current.storageAvailable).toBe(false))
    expect(result.current.state.messages).toEqual([])
  })
})

function createIds() {
  let current = 0
  return () => {
    current += 1
    return `message-test-${current}`
  }
}

function createStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem(key: string) {
      return values.get(key) ?? null
    },
    setItem(key: string, value: string) {
      values.set(key, value)
    },
    removeItem(key: string) {
      values.delete(key)
    },
    dump() {
      return Object.fromEntries(values)
    },
  }
}
