import { describe, expect, it, vi } from "vitest"

import {
  V2ApiError,
  buildV2ChatStreamRequest,
  createV2ApiClient,
  parseV2HttpError,
} from "@/lib/v2-api"
import {
  encodeV2SseEvent,
  v2ActionCardWire,
  v2CompletedAt,
  v2CompletedTurnResponseWire,
  v2ConversationSummaryWire,
  v2ErrorResponseWire,
  v2HistoryTurnWire,
  v2PreferenceRecordWire,
  v2RunningTurnResponseWire,
  v2TerminalEventWire,
} from "@/test/v2-fixtures"

const apiKey = "memory-only-secret"

describe("v2 typed API client", () => {
  it("constructs every JSON and stream endpoint with the exact transport contract", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? "GET"

      if (url === "/root/api/v2/me") return jsonResponse({
        principal_id: "alice",
        tenant_id: "tenant_alice",
        allowed_modes: ["shopper"],
        store_id: "demo",
      })
      if (url === "/root/api/v2/conversations" && method === "POST") {
        return jsonResponse({ conversation: v2ConversationSummaryWire }, 201)
      }
      if (url === "/root/api/v2/conversations?mode=shopper&limit=25") {
        return jsonResponse({ conversations: [v2ConversationSummaryWire], next_cursor: null })
      }
      if (url === "/root/api/v2/conversations/conversation_demo_001?limit=10") {
        return jsonResponse({
          conversation: v2ConversationSummaryWire,
          turns: [v2HistoryTurnWire],
        })
      }
      if (url === "/root/api/v2/conversations/conversation_demo_001" && method === "DELETE") {
        return new Response(null, { status: 204 })
      }
      if (url === "/root/api/v2/chat" && method === "POST") {
        return jsonResponse(v2CompletedTurnResponseWire)
      }
      if (url === "/root/api/v2/chat/stream") {
        return streamResponse(encodeV2SseEvent(v2TerminalEventWire))
      }
      if (url === "/root/api/v2/turns/turn_demo_001/cancel") {
        return jsonResponse(v2CompletedTurnResponseWire)
      }
      if (url === "/root/api/v2/turns/turn_demo_001") {
        return jsonResponse(v2CompletedTurnResponseWire)
      }
      if (url === "/root/api/v2/actions/action_checkout_001/confirm") {
        return jsonResponse({
          action_id: "action_checkout_001",
          status: "executed",
          resource_id: "order_demo_001",
          resource_version: 1,
          reused_result: false,
        })
      }
      if (url === "/root/api/v2/actions/action_checkout_001/reject") {
        return jsonResponse({
          action_id: "action_checkout_001",
          proposal_version: 1,
          status: "rejected",
          decided_at: v2CompletedAt,
          reused_result: false,
        })
      }
      if (url === "/root/api/v2/actions/action_checkout_001") {
        return jsonResponse({ action: v2ActionCardWire, result: null })
      }
      if (url === "/root/api/v2/memory?mode=shopper" && method === "GET") {
        return jsonResponse({ preferences: [v2PreferenceRecordWire] })
      }
      if (url === "/root/api/v2/memory" && method === "PUT") {
        return jsonResponse(v2PreferenceRecordWire)
      }
      if (url === "/root/api/v2/memory?mode=shopper" && method === "DELETE") {
        return new Response(null, { status: 204 })
      }
      throw new Error(`unexpected test request: ${method} ${url}`)
    })
    const client = createV2ApiClient({ baseUrl: "/root/", fetchImpl })
    const signal = new AbortController().signal
    const context = { apiKey, signal }
    const chatRequest = {
      conversationId: "conversation_demo_001",
      clientTurnId: "browser:turn-1",
      message: "Tìm sách lịch sử",
    } as const

    const me = await client.getMe(context)
    const created = await client.createConversation({ ...context, mode: "shopper" })
    const listed = await client.listConversations({ ...context, mode: "shopper", limit: 25 })
    const detail = await client.getConversation({
      ...context,
      conversationId: "conversation_demo_001",
      limit: 10,
    })
    await client.deleteConversation({ ...context, conversationId: "conversation_demo_001" })
    const chat = await client.chat({ ...context, request: chatRequest })
    const terminal = await client.streamChat({ ...context, request: chatRequest })
    await client.getTurn({ ...context, turnId: "turn_demo_001" })
    await client.cancelTurn({ ...context, turnId: "turn_demo_001" })
    await client.getAction({ ...context, actionId: "action_checkout_001" })
    await client.confirmAction({
      ...context,
      actionId: "action_checkout_001",
      idempotencyKey: "confirm-key-0001",
      request: { proposalVersion: 1 },
    })
    await client.rejectAction({
      ...context,
      actionId: "action_checkout_001",
      request: { proposalVersion: 1, reason: "Không mua nữa" },
    })
    await client.listPreferences({ ...context, mode: "shopper" })
    await client.putPreference({
      ...context,
      request: {
        sourceTurnId: "turn_demo_001",
        preference: { kind: "genre", value: "Lịch sử" },
      },
    })
    await client.deletePreference({
      ...context,
      mode: "shopper",
      request: { preferenceId: "preference_demo_001" },
    })

    expect(fetchImpl).toHaveBeenCalledTimes(15)
    expect(me).toMatchObject({ principalId: "alice", allowedModes: ["shopper"] })
    expect(created.conversation.conversationId).toBe("conversation_demo_001")
    expect(listed.conversations).toHaveLength(1)
    expect(detail.turns[0].assistantResult?.answer).toContain("kiểm chứng")
    expect(chat.turn.turnId).toBe("turn_demo_001")
    expect(terminal.turnId).toBe("turn_demo_001")

    for (const [, init] of fetchImpl.mock.calls) {
      const headers = new Headers(init?.headers)
      expect(headers.get("X-API-Key")).toBe(apiKey)
      expect(init?.signal).toBe(signal)
      expect(init?.credentials).toBe("same-origin")
      expect(init?.cache).toBe("no-store")
    }
    expect(JSON.stringify(client)).not.toContain(apiKey)

    const chatCall = fetchImpl.mock.calls.find(([url]) => url === "/root/api/v2/chat")
    expect(chatCall?.[1]?.body).toBe(JSON.stringify({
      conversation_id: "conversation_demo_001",
      client_turn_id: "browser:turn-1",
      message: "Tìm sách lịch sử",
    }))
    const confirmCall = fetchImpl.mock.calls.find(([url]) =>
      String(url).endsWith("/actions/action_checkout_001/confirm"),
    )
    expect(new Headers(confirmCall?.[1]?.headers).get("Idempotency-Key"))
      .toBe("confirm-key-0001")
    expect(confirmCall?.[1]?.body).toBe('{"proposal_version":1}')
    const deleteMemoryCall = fetchImpl.mock.calls.find(([url, init]) =>
      String(url).includes("/memory?") && init?.method === "DELETE",
    )
    expect(deleteMemoryCall?.[1]?.body).toBe('{"preference_id":"preference_demo_001"}')
  })

  it("accepts the documented 202 running chat response", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(v2RunningTurnResponseWire, 202),
    )
    const response = await createV2ApiClient({ fetchImpl }).chat({
      apiKey,
      request: {
        conversationId: "conversation_demo_001",
        clientTurnId: "browser:turn-1",
        message: "Tìm sách lịch sử",
      },
    })

    expect(response.turn.status).toBe("running")
    expect(response.result).toBeNull()
  })

  it("builds a stream request without retaining the API key or authority fields", () => {
    const signal = new AbortController().signal
    const built = buildV2ChatStreamRequest({
      baseUrl: "https://example.test/",
      apiKey,
      signal,
      request: {
        conversationId: "conversation_demo_001",
        clientTurnId: "browser:turn-1",
        message: "Tìm sách lịch sử",
      },
    })

    expect(built.url).toBe("https://example.test/api/v2/chat/stream")
    expect(built.init).toMatchObject({
      method: "POST",
      signal,
      body: JSON.stringify({
        conversation_id: "conversation_demo_001",
        client_turn_id: "browser:turn-1",
        message: "Tìm sách lịch sử",
      }),
    })
    expect(new Headers(built.init.headers)).toEqual(expect.objectContaining({}))
    expect(new Headers(built.init.headers).get("Accept")).toBe("text/event-stream")
    expect(new Headers(built.init.headers).get("X-API-Key")).toBe(apiKey)
    expect(String(built.init.body)).not.toContain("authority")
  })

  it("preserves validated safe errors and sanitizes malformed responses", async () => {
    await expect(parseV2HttpError(jsonResponse(v2ErrorResponseWire, 503)))
      .resolves.toMatchObject({
        source: "server",
        code: "v2.runtime_unavailable",
        retryable: true,
        requestId: "request_v2_001",
        httpStatus: 503,
      })

    const malformed = await parseV2HttpError(new Response(
      "PRIVATE SERVER STACK AND SECRET",
      { status: 502 },
    ))
    expect(malformed).toMatchObject({
      source: "client",
      code: "v2.http_502",
      message: "Không thể xử lý yêu cầu lúc này.",
    })
    expect(String(malformed)).not.toContain("PRIVATE SERVER STACK")

    const invalidSuccess = createV2ApiClient({
      fetchImpl: vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
        ...v2CompletedTurnResponseWire,
        raw_prompt: "PRIVATE",
      })),
    })
    await expect(invalidSuccess.chat({
      apiKey,
      request: {
        conversationId: "conversation_demo_001",
        clientTurnId: "browser:turn-1",
        message: "Tìm sách",
      },
    })).rejects.toMatchObject({ code: "v2.invalid_response" })
  })

  it("preserves AbortError, maps network failures safely, and validates before fetch", async () => {
    const abortError = new DOMException("cancelled", "AbortError")
    const aborted = createV2ApiClient({
      fetchImpl: vi.fn<typeof fetch>().mockRejectedValue(abortError),
    })
    await expect(aborted.getMe({ apiKey })).rejects.toBe(abortError)

    const privateFailure = new TypeError("PRIVATE NETWORK DETAIL")
    const failed = createV2ApiClient({
      fetchImpl: vi.fn<typeof fetch>().mockRejectedValue(privateFailure),
    })
    const error = await failed.getMe({ apiKey }).catch((reason: unknown) => reason)
    expect(error).toBeInstanceOf(V2ApiError)
    expect(error).toMatchObject({ code: "v2.network_error", retryable: true })
    expect(String(error)).not.toContain("PRIVATE NETWORK DETAIL")

    const fetchImpl = vi.fn<typeof fetch>()
    const client = createV2ApiClient({ fetchImpl })
    await expect(client.chat({
      apiKey,
      request: {
        conversationId: "conversation_demo_001",
        clientTurnId: "browser:turn-1",
        message: " ",
      },
    })).rejects.toMatchObject({ code: "v2.invalid_request" })
    await expect(client.confirmAction({
      apiKey,
      actionId: "action_checkout_001",
      idempotencyKey: "short",
      request: { proposalVersion: 1 },
    })).rejects.toMatchObject({ code: "v2.invalid_idempotency_key" })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

function streamResponse(text: string): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text))
      controller.close()
    },
  })
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "X-Request-ID": "request_v2_001",
      "X-Trace-ID": "trace_v2_001",
    },
  })
}
