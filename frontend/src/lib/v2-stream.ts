import {
  MAX_V2_SSE_EVENTS,
  turnSseEventSchema,
  type TurnSseEvent,
  type TurnSseProgressEvent,
  type TurnSseTerminalEvent,
  type TurnSseTextDeltaEvent,
} from "@/lib/v2-contracts"

const STREAM_PROTOCOL_MESSAGE = "Phản hồi luồng v2 không đúng hợp đồng."

export interface RawV2SseFrame {
  event: string
  data?: string
  id?: string
  retry?: number
}

export interface V2StreamCallbacks {
  onEvent?: (event: TurnSseEvent) => void
  onProgress?: (event: TurnSseProgressEvent) => void
  onTextDelta?: (event: TurnSseTextDeltaEvent) => void
  onTerminal?: (event: TurnSseTerminalEvent) => void
}

interface V2StreamErrorOptions {
  requestId?: string
  traceId?: string
  turnId?: string
}

export class V2StreamError extends Error {
  readonly code: string
  readonly requestId?: string
  readonly traceId?: string
  readonly turnId?: string
  readonly recoverable = false

  constructor(
    code: string,
    message = STREAM_PROTOCOL_MESSAGE,
    options: V2StreamErrorOptions = {},
  ) {
    super(message)
    this.name = "V2StreamError"
    this.code = code
    this.requestId = options.requestId
    this.traceId = options.traceId
    this.turnId = options.turnId
  }
}

export class V2IncompleteStreamError extends Error {
  readonly code = "v2.stream.incomplete"
  readonly recoverable = true
  readonly requestId?: string
  readonly traceId?: string
  readonly turnId?: string

  constructor(options: V2StreamErrorOptions = {}) {
    super("Kết nối kết thúc trước sự kiện cuối; có thể tải lại lượt trả lời.")
    this.name = "V2IncompleteStreamError"
    this.requestId = options.requestId
    this.traceId = options.traceId
    this.turnId = options.turnId
  }
}

export class V2SseFrameDecoder {
  private buffer = ""
  private pendingCarriageReturn = false

  push(chunk: string): RawV2SseFrame[] {
    this.buffer += this.normalizeChunk(chunk)
    return this.drain()
  }

  finish(): RawV2SseFrame[] {
    if (this.pendingCarriageReturn) {
      this.buffer += "\n"
      this.pendingCarriageReturn = false
    }
    const frames = this.drain()
    if (this.buffer.length > 0) {
      const residual = this.buffer
      this.buffer = ""
      if (parseV2SseBlock(residual)) {
        throw streamProtocolError("v2.stream.truncated_frame")
      }
    }
    return frames
  }

  private normalizeChunk(chunk: string): string {
    let normalized = ""
    let index = 0

    if (this.pendingCarriageReturn) {
      normalized += "\n"
      this.pendingCarriageReturn = false
      if (chunk.startsWith("\n")) index = 1
    }

    while (index < chunk.length) {
      if (chunk[index] !== "\r") {
        normalized += chunk[index]
        index += 1
        continue
      }
      if (index === chunk.length - 1) {
        this.pendingCarriageReturn = true
        break
      }
      normalized += "\n"
      index += chunk[index + 1] === "\n" ? 2 : 1
    }
    return normalized
  }

  private drain(): RawV2SseFrame[] {
    const frames: RawV2SseFrame[] = []
    let boundary = this.buffer.indexOf("\n\n")
    while (boundary >= 0) {
      const block = this.buffer.slice(0, boundary)
      this.buffer = this.buffer.slice(boundary + 2)
      const frame = parseV2SseBlock(block)
      if (frame) frames.push(frame)
      boundary = this.buffer.indexOf("\n\n")
    }
    return frames
  }
}

export function parseV2SseBlock(block: string): RawV2SseFrame | null {
  let event = "message"
  let id: string | undefined
  let retry: number | undefined
  const dataLines: string[] = []

  for (const line of block.split("\n")) {
    if (line.length === 0 || line.startsWith(":")) continue
    const separator = line.indexOf(":")
    const field = separator < 0 ? line : line.slice(0, separator)
    const rawValue = separator < 0 ? "" : line.slice(separator + 1)
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue

    if (field === "event") event = value
    else if (field === "data") dataLines.push(value)
    else if (field === "id" && !value.includes("\0")) id = value
    else if (field === "retry" && /^\d+$/.test(value)) {
      const parsed = Number(value)
      if (Number.isSafeInteger(parsed)) retry = parsed
    }
  }

  if (dataLines.length === 0) return null
  return {
    event,
    data: dataLines.join("\n"),
    ...(id === undefined ? {} : { id }),
    ...(retry === undefined ? {} : { retry }),
  }
}

export function parseV2SseFrame(frame: RawV2SseFrame): TurnSseEvent {
  if (frame.data === undefined) {
    throw streamProtocolError("v2.stream.missing_data")
  }

  let payload: unknown
  try {
    payload = JSON.parse(frame.data)
  } catch {
    throw streamProtocolError("v2.stream.invalid_json")
  }
  const parsed = turnSseEventSchema.safeParse(payload)
  if (!parsed.success) {
    throw streamProtocolError("v2.stream.invalid_event")
  }
  if (frame.event !== parsed.data.event) {
    throw streamProtocolError("v2.stream.event_mismatch")
  }
  if (frame.id === undefined || frame.id !== String(parsed.data.sequence)) {
    throw streamProtocolError("v2.stream.invalid_event_id")
  }
  return parsed.data
}

export async function readV2TurnStream(
  response: Response,
  callbacks: V2StreamCallbacks = {},
): Promise<TurnSseTerminalEvent> {
  const contentType = response.headers.get("content-type") ?? ""
  if (!contentType.toLowerCase().startsWith("text/event-stream")) {
    throw streamProtocolError("v2.stream.invalid_content_type")
  }
  if (!response.body) {
    throw streamProtocolError("v2.stream.unavailable")
  }

  const reader = response.body.getReader()
  const textDecoder = new TextDecoder()
  const frameDecoder = new V2SseFrameDecoder()
  let requestId = response.headers.get("x-request-id") ?? undefined
  let traceId = response.headers.get("x-trace-id") ?? undefined
  let turnId: string | undefined
  let lastSequence = 0
  let eventCount = 0
  let terminal: TurnSseTerminalEvent | undefined

  const consume = (frames: readonly RawV2SseFrame[]) => {
    for (const frame of frames) {
      if (terminal) {
        throw streamProtocolError("v2.stream.data_after_terminal", {
          requestId,
          traceId,
          turnId,
        })
      }
      const event = parseV2SseFrame(frame)
      eventCount += 1
      if (eventCount > MAX_V2_SSE_EVENTS) {
        throw streamProtocolError("v2.stream.too_many_events")
      }
      if (event.sequence <= lastSequence) {
        throw streamProtocolError("v2.stream.invalid_sequence")
      }
      lastSequence = event.sequence
      requestId = assertSameCorrelation("request_id", requestId, event.requestId)
      traceId = assertSameCorrelation("trace_id", traceId, event.traceId)
      turnId = assertSameCorrelation("turn_id", turnId, event.turnId)

      callbacks.onEvent?.(event)
      if (event.event === "progress") callbacks.onProgress?.(event)
      else if (event.event === "text_delta") callbacks.onTextDelta?.(event)
      else {
        terminal = event
        callbacks.onTerminal?.(event)
      }
    }
  }

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (value) consume(frameDecoder.push(textDecoder.decode(value, { stream: true })))
      if (!done) continue
      const finalText = textDecoder.decode()
      if (finalText) consume(frameDecoder.push(finalText))
      consume(frameDecoder.finish())
      break
    }
  } finally {
    try {
      await reader.cancel()
    } catch {
      // A closed or aborted response stream needs no further cleanup.
    }
    reader.releaseLock()
  }

  if (!terminal) {
    throw new V2IncompleteStreamError({ requestId, traceId, turnId })
  }
  return terminal
}

function assertSameCorrelation(
  field: "request_id" | "trace_id" | "turn_id",
  expected: string | undefined,
  received: string,
): string {
  if (expected !== undefined && expected !== received) {
    throw streamProtocolError(`v2.stream.mismatched_${field}`)
  }
  return received
}

function streamProtocolError(
  code: string,
  options: V2StreamErrorOptions = {},
): V2StreamError {
  return new V2StreamError(code, STREAM_PROTOCOL_MESSAGE, options)
}
