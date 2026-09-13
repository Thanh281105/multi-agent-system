import { act, renderHook, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { useChatController } from "@/features/chat/use-chat-controller"
import { writeDurableChatMetadata } from "@/features/chat/chat-storage"
import { GatewayClientError } from "@/lib/gateway-stream"
import { V2ApiError, type V2ApiClient } from "@/lib/v2-api"
import type {
  ActionCard,
  ConversationSummary,
  MeResponse,
  TurnSseEvent,
} from "@/lib/v2-contracts"
import { V2IncompleteStreamError } from "@/lib/v2-stream"
import { completedResponse, statusEvent, tokenEvent } from "@/test/fixtures"
import {
  v2ActionCard,
  v2CompletedTurnResponse,
  v2ConversationSummary,
  v2HistoryTurn,
  v2PreferenceRecord,
  v2ProgressEvents,
  v2RunningTurnResponse,
  v2TerminalEvent,
} from "@/test/v2-fixtures"

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

describe("useChatController durable v2", () => {
  it("gates bootstrap on credentials and hydrates server-owned history", async () => {
    const storage = createStorage()
    const api = createV2Api({
      getConversation: vi.fn(async () => ({
        conversation: v2ConversationSummary,
        turns: [v2HistoryTurn],
      })),
      listPreferences: vi.fn(async () => ({
        preferences: [v2PreferenceRecord],
      })),
    })
    const { result } = renderHook(() =>
      useChatController({
        storage,
        v2Api: api,
        createStorageScopeId: () => "scope-test",
      }),
    )

    await expect(result.current.durable.bootstrap()).resolves.toBe(
      "credential_required",
    )
    expect(api.getMe).not.toHaveBeenCalled()
    act(() => expect(result.current.configureCredential("v2-secret-key")).toBe(true))
    expect(api.getMe).not.toHaveBeenCalled()

    await act(async () => {
      expect(await result.current.durable.bootstrap()).toBe("completed")
    })

    expect(api.getMe).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
    })
    expect(api.listConversations).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      mode: "shopper",
    })
    expect(result.current.durable).toMatchObject({
      identity: v2Identity,
      allowedModes: ["shopper"],
      selectedMode: "shopper",
      phase: "ready",
    })
    expect(result.current.state.durable.history).toEqual([v2HistoryTurn])
    expect(
      result.current.state.durable.resources.actions[v2ActionCard.actionId],
    ).toEqual(v2ActionCard)
    expect(
      result.current.state.durable.resources.preferences[
        v2PreferenceRecord.preferenceId
      ],
    ).toEqual(v2PreferenceRecord)
    const persisted = JSON.stringify(storage.dump())
    expect(persisted).not.toContain("v2-secret-key")
    expect(persisted).not.toContain(v2Identity.principalId)
    expect(persisted).not.toContain(v2Identity.tenantId)
  })

  it("enforces allowed modes before issuing a mode-scoped request", async () => {
    const api = createV2Api()
    const { result } = renderHook(() =>
      useChatController({ storage: createStorage(), v2Api: api }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => {
      expect(await result.current.durable.bootstrap()).toBe("completed")
    })
    const callsBefore = vi.mocked(api.listConversations).mock.calls.length

    await expect(result.current.durable.changeMode("merchant")).resolves.toBe(
      "invalid_mode",
    )
    expect(vi.mocked(api.listConversations)).toHaveBeenCalledTimes(callsBefore)
    expect(result.current.durable.selectedMode).toBe("shopper")
  })

  it("creates, switches, lists, and deletes conversations by exact identity", async () => {
    const second = conversation("conversation_demo_002")
    const created = conversation("conversation_demo_003")
    const api = createV2Api({
      listConversations: vi.fn(async () => ({
        conversations: [v2ConversationSummary, second],
        nextCursor: null,
      })),
      createConversation: vi.fn(async () => ({ conversation: created })),
      getConversation: vi.fn(async ({ conversationId }) => ({
        conversation: [v2ConversationSummary, second, created].find(
          (item) => item.conversationId === conversationId,
        )!,
        turns: conversationId === v2ConversationSummary.conversationId
          ? [v2HistoryTurn]
          : [],
      })),
    })
    const { result } = renderHook(() =>
      useChatController({ storage: createStorage(), v2Api: api }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => {
      await result.current.durable.bootstrap()
      expect(await result.current.durable.listConversations()).toEqual([
        v2ConversationSummary,
        second,
      ])
      expect(await result.current.durable.switchConversation(second.conversationId)).toBe(
        "completed",
      )
    })
    expect(result.current.state.durable.activeConversation).toEqual(second)

    let newConversation: ConversationSummary | null = null
    await act(async () => {
      newConversation = await result.current.durable.createConversation()
    })
    expect(newConversation).toEqual(created)
    expect(api.createConversation).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      mode: "shopper",
    })
    await act(async () => {
      expect(await result.current.durable.deleteConversation(created.conversationId)).toBe(
        "completed",
      )
    })
    expect(api.deleteConversation).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      conversationId: created.conversationId,
    })
    expect(result.current.state.durable.activeConversation?.conversationId).toBe(
      v2ConversationSummary.conversationId,
    )
  })

  it("reconciles an incomplete stream and retries the same chat identity once", async () => {
    const streamChat = vi
      .fn<V2ApiClient["streamChat"]>()
      .mockImplementationOnce(async (options) => {
        options.onEvent?.(v2ProgressEvents[0])
        throw new V2IncompleteStreamError({ turnId: "turn_demo_001" })
      })
      .mockImplementationOnce(async (options) => {
        options.onEvent?.(v2TerminalEvent)
        return v2TerminalEvent
      })
    const api = createV2Api({ streamChat })
    const createClientTurnId = vi.fn(() => "browser:turn-1")
    const { result } = renderHook(() =>
      useChatController({
        storage: createStorage(),
        v2Api: api,
        createClientTurnId,
      }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => void (await result.current.durable.bootstrap()))

    await act(async () => {
      expect(await result.current.durable.sendMessage("  Tư vấn sách  ")).toBe(
        "completed",
      )
    })
    expect(createClientTurnId).toHaveBeenCalledTimes(1)
    expect(streamChat).toHaveBeenCalledTimes(2)
    for (const [options] of streamChat.mock.calls) {
      expect(options.request).toEqual({
        conversationId: v2ConversationSummary.conversationId,
        clientTurnId: "browser:turn-1",
        message: "Tư vấn sách",
      })
    }
    expect(api.getTurn).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      turnId: "turn_demo_001",
    })
    expect(result.current.state.durable.terminalResult).toEqual(
      v2CompletedTurnResponse.result,
    )
  })

  it("resumes metadata-only recovery after reload without creating a new turn id", async () => {
    const storage = createStorage()
    writeDurableChatMetadata(
      storage,
      { scopeId: "scope-test", mode: "shopper" },
      {
        selectedConversationId: v2ConversationSummary.conversationId,
        pendingRecovery: {
          conversationId: v2ConversationSummary.conversationId,
          clientTurnId: "browser:restored-turn",
          message: "Khôi phục câu hỏi",
        },
      },
    )
    const streamChat = vi.fn<V2ApiClient["streamChat"]>(async (options) => {
      options.onEvent?.(v2TerminalEvent)
      return v2TerminalEvent
    })
    const createClientTurnId = vi.fn(() => "browser:new-turn")
    const api = createV2Api({ streamChat })
    const { result } = renderHook(() =>
      useChatController({
        storage,
        v2Api: api,
        createClientTurnId,
        createStorageScopeId: () => "scope-test",
      }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => {
      expect(await result.current.durable.bootstrap()).toBe("completed")
    })
    expect(createClientTurnId).not.toHaveBeenCalled()
    expect(streamChat).toHaveBeenCalledWith(
      expect.objectContaining({
        request: {
          conversationId: v2ConversationSummary.conversationId,
          clientTurnId: "browser:restored-turn",
          message: "Khôi phục câu hỏi",
        },
      }),
    )
  })

  it("cancels durably and ignores a stale stream terminal", async () => {
    let finishStream: (() => void) | undefined
    let staleCallback: ((event: TurnSseEvent) => void) | undefined
    const streamChat = vi.fn<V2ApiClient["streamChat"]>(
      (options) => new Promise((resolve) => {
        staleCallback = options.onEvent
        options.onEvent?.(v2ProgressEvents[0])
        finishStream = () => resolve(v2TerminalEvent)
      }),
    )
    const api = createV2Api({
      streamChat,
      cancelTurn: vi.fn(async () => v2CompletedTurnResponse),
    })
    const { result } = renderHook(() =>
      useChatController({
        storage: createStorage(),
        v2Api: api,
        createClientTurnId: () => "browser:turn-1",
      }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => void (await result.current.durable.bootstrap()))
    let sendPromise: Promise<string>
    act(() => {
      sendPromise = result.current.durable.sendMessage("Chuẩn bị đơn hàng")
    })
    await waitFor(() => {
      expect(result.current.state.durable.activeTurn?.turnId).toBe("turn_demo_001")
    })
    await act(async () => {
      const response = await result.current.durable.cancelRequest()
      expect(response?.turn.status).toBe("completed")
    })
    expect(api.cancelTurn).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      turnId: "turn_demo_001",
    })
    act(() => {
      staleCallback?.(v2TerminalEvent)
      finishStream?.()
    })
    await act(async () => expect(await sendPromise).toBe("cancelled"))
    expect(result.current.state.durable.activeTurn?.status).toBe("completed")
    expect(result.current.state.durable.resources.actions[v2ActionCard.actionId]).toEqual(
      v2ActionCard,
    )
  })

  it("waits for admission before cancelling a turn without a server id", async () => {
    let emitAdmission: (() => void) | undefined
    const streamChat = vi.fn<V2ApiClient["streamChat"]>(
      (options) => new Promise((_resolve, reject) => {
        emitAdmission = () => options.onEvent?.(v2ProgressEvents[0])
        options.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"))
        })
      }),
    )
    const cancelTurn = vi.fn<V2ApiClient["cancelTurn"]>(
      async () => v2CompletedTurnResponse,
    )
    const api = createV2Api({ streamChat, cancelTurn })
    const { result } = renderHook(() =>
      useChatController({
        storage: createStorage(),
        v2Api: api,
        createClientTurnId: () => "browser:cancel-before-admission",
      }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => void (await result.current.durable.bootstrap()))

    let sendPromise!: Promise<string>
    act(() => {
      sendPromise = result.current.durable.sendMessage("Huỷ ngay khi gửi")
    })
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1))
    let cancelPromise!: Promise<unknown>
    act(() => {
      cancelPromise = result.current.durable.cancelRequest()
    })
    expect(cancelTurn).not.toHaveBeenCalled()

    act(() => emitAdmission?.())
    await act(async () => void (await cancelPromise))
    expect(cancelTurn).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      turnId: "turn_demo_001",
    })
    await act(async () => expect(await sendPromise).toBe("cancelled"))
  })

  it("reads back an ambiguous confirm and reuses one idempotency key", async () => {
    const executedAction: ActionCard = { ...v2ActionCard, status: "executed" }
    const terminalReadback = {
      action: executedAction,
      result: {
        actionId: executedAction.actionId,
        status: "executed" as const,
        resourceId: executedAction.target.resourceId,
        resourceVersion: 4,
        reusedResult: false,
      },
    }
    const getAction = vi
      .fn<V2ApiClient["getAction"]>()
      .mockResolvedValueOnce({ action: v2ActionCard, result: null })
      .mockResolvedValue(terminalReadback)
    const confirmAction = vi
      .fn<V2ApiClient["confirmAction"]>()
      .mockRejectedValueOnce(
        new V2ApiError("v2.network_error", "Mất kết nối", { retryable: true }),
      )
      .mockResolvedValueOnce(terminalReadback.result)
    const createIdempotencyKey = vi.fn(() => "confirm-intent-001")
    const api = createV2Api({
      getConversation: vi.fn(async () => ({
        conversation: v2ConversationSummary,
        turns: [v2HistoryTurn],
      })),
      getAction,
      confirmAction,
    })
    const { result } = renderHook(() =>
      useChatController({
        storage: createStorage(),
        v2Api: api,
        createIdempotencyKey,
      }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => void (await result.current.durable.bootstrap()))

    await act(async () => {
      expect(
        await result.current.durable.confirmAction(v2ActionCard.actionId, 1),
      ).toEqual(terminalReadback)
    })
    expect(createIdempotencyKey).toHaveBeenCalledTimes(1)
    expect(confirmAction).toHaveBeenCalledTimes(2)
    const keys = confirmAction.mock.calls.map(([options]) => options.idempotencyKey)
    expect(keys).toEqual(["confirm-intent-001", "confirm-intent-001"])
    expect(getAction).toHaveBeenCalledTimes(2)
    expect(
      result.current.state.durable.resources.actions[v2ActionCard.actionId].status,
    ).toBe("executed")
    await act(async () => {
      expect(
        await result.current.durable.confirmAction(v2ActionCard.actionId, 1),
      ).toEqual(terminalReadback)
    })
    expect(confirmAction).toHaveBeenCalledTimes(2)
  })

  it("reads and rejects a proposed action, then keeps its terminal status", async () => {
    const rejectedAction: ActionCard = { ...v2ActionCard, status: "rejected" }
    const rejectedReadback = {
      action: rejectedAction,
      result: {
        actionId: rejectedAction.actionId,
        proposalVersion: rejectedAction.proposalVersion,
        status: "rejected" as const,
        decidedAt: "2026-09-09T08:02:00Z",
        reusedResult: false,
      },
    }
    const getAction = vi
      .fn<V2ApiClient["getAction"]>()
      .mockResolvedValueOnce({ action: v2ActionCard, result: null })
      .mockResolvedValue(rejectedReadback)
    const api = createV2Api({
      getConversation: vi.fn(async () => ({
        conversation: v2ConversationSummary,
        turns: [v2HistoryTurn],
      })),
      getAction,
    })
    const { result } = renderHook(() =>
      useChatController({ storage: createStorage(), v2Api: api }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => void (await result.current.durable.bootstrap()))

    await act(async () => {
      expect(await result.current.durable.getAction(v2ActionCard.actionId)).toEqual({
        action: v2ActionCard,
        result: null,
      })
      expect(
        await result.current.durable.rejectAction(
          v2ActionCard.actionId,
          v2ActionCard.proposalVersion,
          "Không mua nữa",
        ),
      ).toEqual(rejectedReadback)
    })
    expect(api.rejectAction).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      actionId: v2ActionCard.actionId,
      request: { proposalVersion: 1, reason: "Không mua nữa" },
    })
    expect(
      result.current.state.durable.resources.actions[v2ActionCard.actionId].status,
    ).toBe("rejected")
  })

  it("reconciles an ambiguous reject without sending a second decision", async () => {
    const rejectedAction: ActionCard = { ...v2ActionCard, status: "rejected" }
    const rejectedReadback = {
      action: rejectedAction,
      result: {
        actionId: rejectedAction.actionId,
        proposalVersion: rejectedAction.proposalVersion,
        status: "rejected" as const,
        decidedAt: "2026-09-09T08:02:00Z",
        reusedResult: true,
      },
    }
    const rejectAction = vi.fn<V2ApiClient["rejectAction"]>(async () => {
      throw new V2ApiError("v2.network_error", "Mất kết nối sau khi gửi.", {
        retryable: true,
      })
    })
    const getAction = vi
      .fn<V2ApiClient["getAction"]>()
      .mockResolvedValueOnce({ action: v2ActionCard, result: null })
      .mockResolvedValue(rejectedReadback)
    const api = createV2Api({
      getConversation: vi.fn(async () => ({
        conversation: v2ConversationSummary,
        turns: [v2HistoryTurn],
      })),
      getAction,
      rejectAction,
    })
    const { result } = renderHook(() =>
      useChatController({ storage: createStorage(), v2Api: api }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => void (await result.current.durable.bootstrap()))
    await act(async () => {
      await result.current.durable.getAction(v2ActionCard.actionId)
    })

    await act(async () => {
      expect(
        await result.current.durable.rejectAction(
          v2ActionCard.actionId,
          v2ActionCard.proposalVersion,
        ),
      ).toEqual(rejectedReadback)
    })
    expect(rejectAction).toHaveBeenCalledOnce()
    expect(getAction).toHaveBeenCalledTimes(2)
    expect(
      result.current.state.durable.resources.actions[v2ActionCard.actionId].status,
    ).toBe("rejected")
  })

  it("enforces source-turn preference CRUD and refreshes reducer state", async () => {
    const listPreferences = vi
      .fn<V2ApiClient["listPreferences"]>()
      .mockResolvedValueOnce({ preferences: [] })
      .mockResolvedValueOnce({ preferences: [v2PreferenceRecord] })
      .mockResolvedValueOnce({ preferences: [] })
    const api = createV2Api({
      getConversation: vi.fn(async () => ({
        conversation: v2ConversationSummary,
        turns: [v2HistoryTurn],
      })),
      listPreferences,
    })
    const { result } = renderHook(() =>
      useChatController({ storage: createStorage(), v2Api: api }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => void (await result.current.durable.bootstrap()))

    await expect(
      result.current.durable.putPreference("turn_unknown", {
        kind: "genre",
        value: "Lịch sử",
      }),
    ).resolves.toBeNull()
    expect(api.putPreference).not.toHaveBeenCalled()
    await act(async () => {
      expect(
        await result.current.durable.putPreference(v2HistoryTurn.turnId, {
          kind: "genre",
          value: "Lịch sử",
        }),
      ).toEqual(v2PreferenceRecord)
    })
    expect(api.putPreference).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      request: {
        sourceTurnId: v2HistoryTurn.turnId,
        preference: { kind: "genre", value: "Lịch sử" },
      },
    })
    expect(result.current.state.durable.resources.preferences).toHaveProperty(
      v2PreferenceRecord.preferenceId,
    )
    await act(async () => {
      expect(
        await result.current.durable.deletePreference(
          v2PreferenceRecord.preferenceId,
        ),
      ).toBe("completed")
    })
    expect(api.deletePreference).toHaveBeenCalledWith({
      apiKey: "v2-secret-key",
      signal: expect.any(AbortSignal),
      mode: "shopper",
      request: { preferenceId: v2PreferenceRecord.preferenceId },
    })
    expect(result.current.state.durable.resources.preferences).toEqual({})
  })

  it("clears scoped metadata and ignores old callbacks across a mode change", async () => {
    const merchant = conversation("conversation_merchant_001", "merchant")
    let finishStream: (() => void) | undefined
    let staleCallback: ((event: TurnSseEvent) => void) | undefined
    const api = createV2Api({
      getMe: vi.fn<V2ApiClient["getMe"]>(async () => ({
        ...v2Identity,
        allowedModes: ["shopper", "merchant"],
      })),
      listConversations: vi.fn(async ({ mode }) => ({
        conversations: mode === "shopper" ? [v2ConversationSummary] : [merchant],
        nextCursor: null,
      })),
      getConversation: vi.fn(async ({ conversationId }) => ({
        conversation:
          conversationId === merchant.conversationId
            ? merchant
            : v2ConversationSummary,
        turns: [],
      })),
      streamChat: vi.fn<V2ApiClient["streamChat"]>(
        (options) => new Promise((resolve) => {
          staleCallback = options.onEvent
          finishStream = () => resolve(v2TerminalEvent)
        }),
      ),
    })
    const storage = createStorage()
    const { result } = renderHook(() =>
      useChatController({
        storage,
        v2Api: api,
        createClientTurnId: () => "browser:turn-1",
        createStorageScopeId: () => "scope-test",
      }),
    )
    act(() => result.current.configureCredential("v2-secret-key"))
    await act(async () => void (await result.current.durable.bootstrap()))
    let sendPromise: Promise<string>
    act(() => {
      sendPromise = result.current.durable.sendMessage("Câu hỏi đang chạy")
    })
    await waitFor(() => expect(api.streamChat).toHaveBeenCalledTimes(1))

    await act(async () => {
      expect(await result.current.durable.changeMode("merchant")).toBe("completed")
    })
    act(() => {
      staleCallback?.(v2TerminalEvent)
      finishStream?.()
    })
    await act(async () => expect(await sendPromise).toBe("cancelled"))
    expect(result.current.durable.selectedMode).toBe("merchant")
    expect(result.current.state.durable.activeConversation).toEqual(merchant)
    expect(result.current.state.durable.terminalResult).toBeNull()
    expect(JSON.parse(storage.dump()["thuong-tri.chat-metadata.v2"])).toMatchObject({
      selectedMode: "merchant",
      selectedConversationId: merchant.conversationId,
      pendingRecovery: null,
    })

    act(() => result.current.clearCredential())
    expect(result.current.durable.identity).toBeNull()
    expect(storage.dump()["thuong-tri.chat-metadata.v2"]).toBeUndefined()
  })
})

const v2Identity: MeResponse = {
  principalId: "principal@example.test",
  tenantId: "tenant_demo",
  allowedModes: ["shopper"],
  storeId: "demo",
}

function conversation(
  conversationId: string,
  mode: "shopper" | "merchant" = "shopper",
): ConversationSummary {
  return {
    ...v2ConversationSummary,
    conversationId,
    mode,
    title: conversationId,
  }
}

function createV2Api(overrides: Partial<V2ApiClient> = {}): V2ApiClient {
  const api: V2ApiClient = {
    getMe: vi.fn(async () => v2Identity),
    createConversation: vi.fn(async ({ mode }) => ({
      conversation: conversation(`conversation_${mode}_created`, mode),
    })),
    listConversations: vi.fn(async () => ({
      conversations: [v2ConversationSummary],
      nextCursor: null,
    })),
    getConversation: vi.fn(async () => ({
      conversation: v2ConversationSummary,
      turns: [],
    })),
    deleteConversation: vi.fn(async () => undefined),
    chat: vi.fn(async () => v2CompletedTurnResponse),
    streamChat: vi.fn<V2ApiClient["streamChat"]>(async (options) => {
      options.onEvent?.(v2TerminalEvent)
      return v2TerminalEvent
    }),
    getTurn: vi.fn(async () => v2RunningTurnResponse),
    cancelTurn: vi.fn(async () => v2CompletedTurnResponse),
    getAction: vi.fn(async () => ({ action: v2ActionCard, result: null })),
    confirmAction: vi.fn<V2ApiClient["confirmAction"]>(async () => ({
      actionId: v2ActionCard.actionId,
      status: "executed",
      resourceId: v2ActionCard.target.resourceId,
      resourceVersion: 4,
      reusedResult: false,
    })),
    rejectAction: vi.fn<V2ApiClient["rejectAction"]>(async () => ({
      actionId: v2ActionCard.actionId,
      proposalVersion: v2ActionCard.proposalVersion,
      status: "rejected",
      decidedAt: "2026-09-09T08:02:00Z",
      reusedResult: false,
    })),
    listPreferences: vi.fn(async () => ({ preferences: [] })),
    putPreference: vi.fn(async () => v2PreferenceRecord),
    deletePreference: vi.fn(async () => undefined),
  }
  return { ...api, ...overrides }
}

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
