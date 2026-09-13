import { describe, expect, it, vi } from "vitest"

import {
  V2IncompleteStreamError,
  V2SseFrameDecoder,
  parseV2SseBlock,
  parseV2SseFrame,
  readV2TurnStream,
} from "@/lib/v2-stream"
import {
  encodeV2SseEvent,
  v2ProgressEventsWire,
  v2TerminalEventWire,
  v2TerminalStatesWire,
  v2TextDeltaEventWire,
} from "@/test/v2-fixtures"

describe("V2SseFrameDecoder", () => {
  it("parses event, id, retry, multiline data, LF, and CRLF", () => {
    const decoder = new V2SseFrameDecoder()
    const block = [
      "id: 7",
      "retry: 3000",
      "event: custom",
      "data: first",
      "data: second",
      "",
      "",
    ].join("\r\n")
    const split = block.indexOf("\r\n\r\n") + 3

    expect(decoder.push(block.slice(0, split))).toEqual([])
    expect(decoder.push(block.slice(split))).toEqual([
      { event: "custom", id: "7", retry: 3_000, data: "first\nsecond" },
    ])
    expect(decoder.finish()).toEqual([])
  })

  it("ignores comment-only heartbeats and unknown SSE fields", () => {
    const decoder = new V2SseFrameDecoder()
    expect(decoder.push(": heartbeat\n\n")).toEqual([])
    expect(parseV2SseBlock("unknown: x\nevent: x\nid: 1")).toBeNull()
  })

  it("rejects an unterminated data frame", () => {
    const decoder = new V2SseFrameDecoder()
    decoder.push("id: 1\nevent: progress\ndata: {}")
    expect(() => decoder.finish()).toThrowError(
      expect.objectContaining({ code: "v2.stream.truncated_frame" }),
    )
  })
})

describe("v2 stream protocol", () => {
  it("preserves Vietnamese text split inside a UTF-8 code point", async () => {
    const vietnamese = {
      ...v2TextDeltaEventWire,
      delta: "Đề xuất đã kiểm chứng.",
    }
    const text = encodeV2SseEvent(vietnamese) + encodeV2SseEvent({
      ...v2TerminalEventWire,
      sequence: 6,
      payload: {
        ...v2TerminalEventWire.payload,
        result: { ...v2TerminalEventWire.payload.result, answer: vietnamese.delta },
      },
    })
    const bytes = new TextEncoder().encode(text)
    const marker = new TextEncoder().encode("Đ")[0]
    const splitAt = bytes.indexOf(marker) + 1
    const deltas: string[] = []

    const terminal = await readV2TurnStream(
      streamResponse([bytes.slice(0, splitAt), bytes.slice(splitAt)]),
      { onTextDelta: (event) => deltas.push(event.delta) },
    )

    expect(deltas).toEqual(["Đề xuất đã kiểm chứng."])
    expect(terminal.payload.status).toBe("completed")
    if (terminal.payload.status === "completed") {
      expect(terminal.payload.result.answer).toBe("Đề xuất đã kiểm chứng.")
    }
  })

  it("ignores comments and delivers normalized progress, delta, and terminal events", async () => {
    const onProgress = vi.fn()
    const onTextDelta = vi.fn()
    const onTerminal = vi.fn()
    const frames = [
      ": heartbeat\r\n\r\n",
      ...v2ProgressEventsWire.map((event) => encodeV2SseEvent(event, { newline: "\r\n" })),
      encodeV2SseEvent(v2TextDeltaEventWire),
      encodeV2SseEvent(v2TerminalEventWire),
    ].join("")

    const terminal = await readV2TurnStream(textStreamResponse(frames), {
      onProgress,
      onTextDelta,
      onTerminal,
    })

    expect(onProgress).toHaveBeenCalledTimes(4)
    expect(onProgress.mock.calls[0][0]).toMatchObject({
      requestId: "request_v2_001",
      turnStatus: "pending",
    })
    expect(onTextDelta).toHaveBeenCalledWith(
      expect.objectContaining({ postGrounding: true }),
    )
    expect(onTerminal).toHaveBeenCalledOnce()
    expect(terminal).toMatchObject({ turnId: "turn_demo_001", reusedResult: false })
  })

  it("accepts every strict terminal payload state", async () => {
    for (const event of v2TerminalStatesWire) {
      const terminal = await readV2TurnStream(
        textStreamResponse(encodeV2SseEvent(event)),
      )
      expect(terminal.payload.status).toBe(event.payload.status)
    }
  })

  it("rejects duplicate and out-of-order sequences", async () => {
    const duplicate = {
      ...v2ProgressEventsWire[1],
      sequence: v2ProgressEventsWire[0].sequence,
    }
    await expect(readV2TurnStream(textStreamResponse(
      encodeV2SseEvent(v2ProgressEventsWire[0]) + encodeV2SseEvent(duplicate),
    ))).rejects.toMatchObject({ code: "v2.stream.invalid_sequence" })
  })

  it("rejects correlation changes and response-header mismatches", async () => {
    await expect(readV2TurnStream(textStreamResponse(
      encodeV2SseEvent(v2ProgressEventsWire[0]) + encodeV2SseEvent({
        ...v2TerminalEventWire,
        trace_id: "trace_other_001",
      }),
    ))).rejects.toMatchObject({ code: "v2.stream.mismatched_trace_id" })

    await expect(readV2TurnStream(streamResponse(
      [new TextEncoder().encode(encodeV2SseEvent(v2TerminalEventWire))],
      { requestId: "request_other_001" },
    ))).rejects.toMatchObject({ code: "v2.stream.mismatched_request_id" })
  })

  it("rejects mismatched event names, IDs, malformed JSON, and extra fields", () => {
    expect(() => parseV2SseFrame({
      event: "progress",
      id: "6",
      data: JSON.stringify(v2TerminalEventWire),
    })).toThrowError(expect.objectContaining({ code: "v2.stream.event_mismatch" }))
    expect(() => parseV2SseFrame({
      event: "terminal",
      id: "5",
      data: JSON.stringify(v2TerminalEventWire),
    })).toThrowError(expect.objectContaining({ code: "v2.stream.invalid_event_id" }))
    expect(() => parseV2SseFrame({
      event: "terminal",
      id: "6",
      data: "{PRIVATE_INVALID_JSON",
    })).toThrowError(expect.objectContaining({ code: "v2.stream.invalid_json" }))
    expect(() => parseV2SseFrame({
      event: "terminal",
      id: "6",
      data: JSON.stringify({ ...v2TerminalEventWire, authority: "admin" }),
    })).toThrowError(expect.objectContaining({ code: "v2.stream.invalid_event" }))
  })

  it("rejects any data frame after the single terminal event", async () => {
    await expect(readV2TurnStream(textStreamResponse(
      encodeV2SseEvent(v2TerminalEventWire) +
      encodeV2SseEvent({ ...v2TerminalEventWire, sequence: 7 }),
    ))).rejects.toMatchObject({ code: "v2.stream.data_after_terminal" })
  })

  it("returns a recoverable missing-terminal signal without fabricating a result", async () => {
    const error = await readV2TurnStream(textStreamResponse(
      encodeV2SseEvent(v2ProgressEventsWire[0]),
    )).catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(V2IncompleteStreamError)
    expect(error).toMatchObject({
      code: "v2.stream.incomplete",
      recoverable: true,
      requestId: "request_v2_001",
      traceId: "trace_v2_001",
      turnId: "turn_demo_001",
    })
  })

  it("rejects non-SSE and bodyless responses", async () => {
    await expect(readV2TurnStream(new Response("{}", {
      headers: { "Content-Type": "application/json" },
    }))).rejects.toMatchObject({ code: "v2.stream.invalid_content_type" })
    await expect(readV2TurnStream(new Response(null, {
      headers: { "Content-Type": "text/event-stream" },
    }))).rejects.toMatchObject({ code: "v2.stream.unavailable" })
  })
})

function textStreamResponse(text: string): Response {
  return streamResponse([new TextEncoder().encode(text)])
}

function streamResponse(
  chunks: Uint8Array[],
  headers: { requestId?: string; traceId?: string } = {},
): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "X-Request-ID": headers.requestId ?? "request_v2_001",
      "X-Trace-ID": headers.traceId ?? "trace_v2_001",
    },
  })
}
