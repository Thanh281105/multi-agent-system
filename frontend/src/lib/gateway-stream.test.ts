import { describe, expect, it, vi } from "vitest"

import {
  SseFrameDecoder,
  isAbortError,
  parseGatewayFrame,
  parseHttpError,
  parseSseBlock,
  readGatewayStream,
  sendGatewayMessage,
} from "@/lib/gateway-stream"
import {
  completedResponse,
  encodeTestEvent,
  errorResponse,
  statusEvent,
  tokenEvent,
} from "@/test/fixtures"

describe("SseFrameDecoder", () => {
  it("parses the initial status frame with CRLF metadata", () => {
    const decoder = new SseFrameDecoder()
    const text = encodeTestEvent("status", statusEvent, {
      id: "req_test_123:1",
      retry: 3_000,
      newline: "\r\n",
    })

    const frames = decoder.push(text)

    expect(frames).toEqual([
      {
        event: "status",
        id: "req_test_123:1",
        retry: 3_000,
        data: JSON.stringify(statusEvent),
      },
    ])
    expect(parseGatewayFrame(frames[0])).toMatchObject({
      type: "status",
      data: statusEvent,
    })
  })

  it("handles multiple frames and CRLF split between chunks", () => {
    const decoder = new SseFrameDecoder()
    const first = encodeTestEvent("status", statusEvent, {
      newline: "\r\n",
    })
    const second = encodeTestEvent("completed", completedResponse)
    const split = first.indexOf("\r\n\r\n") + 3

    expect(decoder.push(first.slice(0, split))).toEqual([])
    const frames = decoder.push(first.slice(split) + second)

    expect(frames.map((frame) => frame.event)).toEqual([
      "status",
      "completed",
    ])
  })

  it("ignores heartbeat comments and joins data lines", () => {
    const decoder = new SseFrameDecoder()
    expect(decoder.push(": heartbeat\n\n")).toEqual([])
    expect(parseSseBlock("event: custom\ndata: first\ndata: second")).toEqual({
      event: "custom",
      data: "first\nsecond",
    })
  })

  it("rejects an unterminated data frame at EOF", () => {
    const decoder = new SseFrameDecoder()
    decoder.push("event: status\ndata: {}")

    expect(() => decoder.finish()).toThrowError(
      expect.objectContaining({ code: "gateway.truncated_sse_frame" }),
    )
  })

  it("flushes a trailing carriage return without inventing an event", () => {
    const decoder = new SseFrameDecoder()
    expect(decoder.push(": heartbeat\r")).toEqual([])
    expect(decoder.finish()).toEqual([])
  })

  it("sanitizes malformed JSON without echoing frame content", () => {
    const privateText = "PRIVATE-FRAME-CONTENT"

    expect(() =>
      parseGatewayFrame({ event: "status", data: `{${privateText}` }),
    ).toThrowError(
      expect.objectContaining({ code: "gateway.invalid_event_json" }),
    )
    try {
      parseGatewayFrame({ event: "status", data: `{${privateText}` })
    } catch (error) {
      expect(String(error)).not.toContain(privateText)
    }
  })

  it("ignores unknown event names after parsing their JSON", () => {
    expect(
      parseGatewayFrame({ event: "future.event", data: '{"ok":true}' }),
    ).toBeNull()
  })

  it("rejects extra internal fields in a completed payload", () => {
    expect(() =>
      parseGatewayFrame({
        event: "completed",
        data: JSON.stringify({
          ...completedResponse,
          agent_results: ["must-not-cross-boundary"],
        }),
      }),
    ).toThrowError(
      expect.objectContaining({ code: "gateway.invalid_completed_event" }),
    )
  })
})

describe("gateway stream client", () => {
  it("preserves Vietnamese UTF-8 split across byte chunks", async () => {
    const text = encodeTestEvent("completed", {
      ...completedResponse,
      answer: "Đề xuất kiểm chứng được.",
    })
    const encoded = new TextEncoder().encode(text)
    const marker = new TextEncoder().encode("Đề")[0]
    const splitAt = encoded.indexOf(marker) + 1
    const response = streamResponse([
      encoded.slice(0, splitAt),
      encoded.slice(splitAt),
    ])

    const result = await readGatewayStream(response)

    expect(result.answer).toBe("Đề xuất kiểm chứng được.")
  })

  it("delivers ordered status and returns a completed payload", async () => {
    const onStatus = vi.fn()
    const onToken = vi.fn()
    const response = textStreamResponse(
      encodeTestEvent("status", statusEvent) +
        encodeTestEvent("token", tokenEvent) +
        encodeTestEvent("completed", completedResponse),
    )

    const result = await readGatewayStream(response, onStatus, onToken)

    expect(onStatus).toHaveBeenCalledWith(statusEvent)
    expect(onToken).toHaveBeenCalledWith(tokenEvent)
    expect(result).toEqual(completedResponse)
  })

  it("rejects non-increasing token sequences", async () => {
    const duplicateToken = { ...tokenEvent, delta: "again" }

    await expect(
      readGatewayStream(
        textStreamResponse(
          encodeTestEvent("status", statusEvent) +
            encodeTestEvent("token", tokenEvent) +
            encodeTestEvent("token", duplicateToken),
        ),
      ),
    ).rejects.toMatchObject({ code: "gateway.invalid_token_sequence" })
  })

  it("converts a streamed error into a correlated client error", async () => {
    const response = textStreamResponse(
      encodeTestEvent("status", statusEvent) +
        encodeTestEvent("error", errorResponse),
    )

    await expect(readGatewayStream(response)).rejects.toMatchObject({
      code: "gateway.internal_error",
      retryable: true,
      requestId: "req_test_123",
      traceId: "trace_test_123",
      httpStatus: undefined,
    })
  })

  it("rejects missing terminal events and non-SSE content", async () => {
    await expect(
      readGatewayStream(textStreamResponse(encodeTestEvent("status", statusEvent))),
    ).rejects.toMatchObject({ code: "gateway.incomplete_stream" })

    await expect(
      readGatewayStream(new Response("{}", { headers: { "Content-Type": "application/json" } })),
    ).rejects.toMatchObject({ code: "gateway.invalid_stream_content_type" })

    await expect(
      readGatewayStream(
        new Response(null, {
          headers: { "Content-Type": "text/event-stream" },
        }),
      ),
    ).rejects.toMatchObject({ code: "gateway.stream_unavailable" })
  })

  it("rejects correlation changes and non-increasing status sequences", async () => {
    const changedTrace = { ...statusEvent, sequence: 2, trace_id: "trace_other" }
    const duplicateSequence = { ...statusEvent, phase: "routing.completed" }

    await expect(
      readGatewayStream(
        textStreamResponse(
          encodeTestEvent("status", statusEvent) +
            encodeTestEvent("status", changedTrace),
        ),
      ),
    ).rejects.toMatchObject({ code: "gateway.mismatched_trace_id" })

    await expect(
      readGatewayStream(
        textStreamResponse(
          encodeTestEvent("status", statusEvent) +
            encodeTestEvent("status", duplicateSequence),
        ),
      ),
    ).rejects.toMatchObject({ code: "gateway.invalid_status_sequence" })
  })

  it("sends the authenticated POST contract without persisting credentials", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      textStreamResponse(encodeTestEvent("completed", completedResponse)),
    )
    const controller = new AbortController()

    await sendGatewayMessage({
      message: "  Tìm sách chiêm tinh  ",
      sessionId: "sess_test_123",
      apiKey: "memory-only-secret",
      signal: controller.signal,
      fetchImpl,
    })

    expect(fetchImpl).toHaveBeenCalledOnce()
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe("/api/v1/chat/stream")
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      body: JSON.stringify({
        message: "Tìm sách chiêm tinh",
        session_id: "sess_test_123",
      }),
    })
    expect(init?.headers).toEqual(
      expect.objectContaining({
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-API-Key": "memory-only-secret",
      }),
    )
  })

  it("preserves AbortError and maps network failures safely", async () => {
    const controller = new AbortController()
    const abortError = new DOMException("cancelled", "AbortError")
    const abortedFetch = vi.fn<typeof fetch>().mockRejectedValue(abortError)
    const failedFetch = vi.fn<typeof fetch>().mockRejectedValue(
      new TypeError("PRIVATE-NETWORK-DETAIL"),
    )

    await expect(
      sendGatewayMessage({
        message: "Tìm sách học tiếng Anh",
        apiKey: "secret",
        signal: controller.signal,
        fetchImpl: abortedFetch,
      }),
    ).rejects.toBe(abortError)
    expect(isAbortError(abortError)).toBe(true)

    await expect(
      sendGatewayMessage({
        message: "Tìm sách học tiếng Anh",
        apiKey: "secret",
        signal: controller.signal,
        fetchImpl: failedFetch,
      }),
    ).rejects.toMatchObject({
      code: "gateway.network_error",
      retryable: true,
      message: "Không thể kết nối đến Gateway.",
    })
  })

  it("rejects empty and oversized messages before fetch", async () => {
    const fetchImpl = vi.fn<typeof fetch>()
    const signal = new AbortController().signal

    await expect(
      sendGatewayMessage({
        message: "   ",
        apiKey: "secret",
        signal,
        fetchImpl,
      }),
    ).rejects.toMatchObject({ code: "gateway.invalid_message" })
    await expect(
      sendGatewayMessage({
        message: "x".repeat(2_001),
        apiKey: "secret",
        signal,
        fetchImpl,
      }),
    ).rejects.toMatchObject({ code: "gateway.invalid_message" })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})

describe("HTTP gateway errors", () => {
  it("preserves a validated gateway error", async () => {
    const response = new Response(JSON.stringify(errorResponse), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    })

    await expect(parseHttpError(response)).resolves.toMatchObject({
      code: "gateway.internal_error",
      retryable: true,
      httpStatus: 503,
      requestId: "req_test_123",
    })
  })

  it("replaces malformed HTTP errors with a stable safe response", async () => {
    const response = new Response("PRIVATE-SERVER-ERROR", { status: 502 })
    const error = await parseHttpError(response)

    expect(error).toMatchObject({
      code: "gateway.http_502",
      message: "Không thể xử lý yêu cầu lúc này.",
      httpStatus: 502,
    })
    expect(String(error)).not.toContain("PRIVATE-SERVER-ERROR")
  })
})

function textStreamResponse(text: string): Response {
  return streamResponse([new TextEncoder().encode(text)])
}

function streamResponse(chunks: Uint8Array[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "X-Request-ID": "req_test_123",
      "X-Trace-ID": "trace_test_123",
    },
  })
}
