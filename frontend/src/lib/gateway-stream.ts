import {
  gatewayChatResponseSchema,
  gatewayErrorResponseSchema,
  gatewayStatusEventSchema,
  type GatewayChatResponse,
  type GatewayErrorDetail,
  type GatewayStatusEvent,
  type GatewayStreamEvent,
} from "@/lib/contracts"

const CHAT_STREAM_URL = "/api/v1/chat/stream"
const GENERIC_ERROR_MESSAGE = "Không thể xử lý yêu cầu lúc này."

export interface GatewayRequest {
  message: string
  sessionId?: string | null
  apiKey: string
  signal: AbortSignal
  onStatus?: (event: GatewayStatusEvent) => void
  fetchImpl?: typeof fetch
}

export interface RawSseFrame {
  event: string
  data?: string
  id?: string
  retry?: number
}

export class GatewayClientError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly requestId?: string
  readonly traceId?: string
  readonly httpStatus?: number

  constructor(
    code: string,
    message: string,
    options: {
      retryable?: boolean
      requestId?: string
      traceId?: string
      httpStatus?: number
    } = {},
  ) {
    super(message)
    this.name = "GatewayClientError"
    this.code = code
    this.retryable = options.retryable ?? false
    this.requestId = options.requestId
    this.traceId = options.traceId
    this.httpStatus = options.httpStatus
  }
}

export class SseFrameDecoder {
  private buffer = ""
  private pendingCarriageReturn = false

  push(chunk: string): RawSseFrame[] {
    this.buffer += this.normalizeChunk(chunk)
    return this.drain()
  }

  finish(): RawSseFrame[] {
    if (this.pendingCarriageReturn) {
      this.buffer += "\n"
      this.pendingCarriageReturn = false
    }
    const frames = this.drain()
    if (this.buffer.length > 0) {
      const residual = this.buffer
      this.buffer = ""
      if (parseSseBlock(residual)) {
        throw protocolError("gateway.truncated_sse_frame")
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
      if (chunk.startsWith("\n")) {
        index = 1
      }
    }

    while (index < chunk.length) {
      const character = chunk[index]
      if (character !== "\r") {
        normalized += character
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

  private drain(): RawSseFrame[] {
    const frames: RawSseFrame[] = []
    let boundary = this.buffer.indexOf("\n\n")

    while (boundary >= 0) {
      const block = this.buffer.slice(0, boundary)
      this.buffer = this.buffer.slice(boundary + 2)
      const frame = parseSseBlock(block)
      if (frame) frames.push(frame)
      boundary = this.buffer.indexOf("\n\n")
    }

    return frames
  }
}

export function parseSseBlock(block: string): RawSseFrame | null {
  let event = "message"
  let id: string | undefined
  let retry: number | undefined
  const dataLines: string[] = []

  for (const line of block.split("\n")) {
    if (line.length === 0 || line.startsWith(":")) continue
    const separator = line.indexOf(":")
    const field = separator >= 0 ? line.slice(0, separator) : line
    const rawValue = separator >= 0 ? line.slice(separator + 1) : ""
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue

    if (field === "event") event = value
    if (field === "data") dataLines.push(value)
    if (field === "id" && !value.includes("\0")) id = value
    if (field === "retry" && /^\d+$/.test(value)) retry = Number(value)
  }

  if (dataLines.length === 0) return null
  return {
    event,
    data: dataLines.join("\n"),
    ...(id === undefined ? {} : { id }),
    ...(retry === undefined ? {} : { retry }),
  }
}

export function parseGatewayFrame(frame: RawSseFrame): GatewayStreamEvent | null {
  if (!frame.data) return null

  let payload: unknown
  try {
    payload = JSON.parse(frame.data)
  } catch {
    throw protocolError("gateway.invalid_event_json")
  }

  const metadata = {
    ...(frame.id === undefined ? {} : { id: frame.id }),
    ...(frame.retry === undefined ? {} : { retry: frame.retry }),
  }

  if (frame.event === "status") {
    const parsed = gatewayStatusEventSchema.safeParse(payload)
    if (!parsed.success) throw protocolError("gateway.invalid_status_event")
    return { type: "status", data: parsed.data, ...metadata }
  }

  if (frame.event === "completed") {
    const parsed = gatewayChatResponseSchema.safeParse(payload)
    if (!parsed.success) throw protocolError("gateway.invalid_completed_event")
    return { type: "completed", data: parsed.data, ...metadata }
  }

  if (frame.event === "error") {
    const parsed = gatewayErrorResponseSchema.safeParse(payload)
    if (!parsed.success) throw protocolError("gateway.invalid_error_event")
    return { type: "error", data: parsed.data, ...metadata }
  }

  return null
}

export async function sendGatewayMessage(
  request: GatewayRequest,
): Promise<GatewayChatResponse> {
  const message = request.message.trim()
  if (message.length === 0 || message.length > 2_000) {
    throw new GatewayClientError(
      "gateway.invalid_message",
      "Câu hỏi cần từ 1 đến 2.000 ký tự.",
    )
  }

  const fetchImpl = request.fetchImpl ?? fetch
  let response: Response
  try {
    response = await fetchImpl(CHAT_STREAM_URL, {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-API-Key": request.apiKey,
      },
      body: JSON.stringify({
        message,
        ...(request.sessionId ? { session_id: request.sessionId } : {}),
      }),
      signal: request.signal,
      credentials: "same-origin",
      cache: "no-store",
    })
  } catch (error) {
    if (isAbortError(error)) throw error
    throw new GatewayClientError(
      "gateway.network_error",
      "Không thể kết nối đến Gateway.",
      { retryable: true },
    )
  }

  if (!response.ok) throw await parseHttpError(response)
  return readGatewayStream(response, request.onStatus)
}

export async function readGatewayStream(
  response: Response,
  onStatus?: (event: GatewayStatusEvent) => void,
): Promise<GatewayChatResponse> {
  const contentType = response.headers.get("content-type") ?? ""
  if (!contentType.toLowerCase().startsWith("text/event-stream")) {
    throw protocolError("gateway.invalid_stream_content_type")
  }
  if (!response.body) {
    throw new GatewayClientError(
      "gateway.stream_unavailable",
      "Trình duyệt không hỗ trợ stream phản hồi.",
    )
  }

  const expectedRequestId = response.headers.get("x-request-id") ?? undefined
  const expectedTraceId = response.headers.get("x-trace-id") ?? undefined
  const reader = response.body.getReader()
  const textDecoder = new TextDecoder()
  const frameDecoder = new SseFrameDecoder()
  let result: GatewayChatResponse | undefined
  let terminalSeen = false
  let lastSequence = 0
  let streamRequestId = expectedRequestId
  let streamTraceId = expectedTraceId

  const consume = (frames: RawSseFrame[]) => {
    for (const frame of frames) {
      const event = parseGatewayFrame(frame)
      if (!event) continue
      if (terminalSeen) throw protocolError("gateway.duplicate_terminal_event")

      const correlation =
        event.type === "error" ? event.data.error : event.data
      streamRequestId = assertCorrelation(
        "request_id",
        streamRequestId,
        correlation.request_id,
      )
      streamTraceId = assertCorrelation(
        "trace_id",
        streamTraceId,
        correlation.trace_id,
      )

      if (event.type === "status") {
        if (event.data.sequence <= lastSequence) {
          throw protocolError("gateway.invalid_status_sequence")
        }
        lastSequence = event.data.sequence
        onStatus?.(event.data)
        continue
      }

      terminalSeen = true
      if (event.type === "error") throw streamError(event.data.error)
      result = event.data
    }
  }

  try {
    while (!terminalSeen) {
      const { value, done } = await reader.read()
      if (value) {
        consume(frameDecoder.push(textDecoder.decode(value, { stream: !done })))
      }
      if (done) {
        const finalText = textDecoder.decode()
        if (finalText) consume(frameDecoder.push(finalText))
        consume(frameDecoder.finish())
        break
      }
    }
  } finally {
    try {
      await reader.cancel()
    } catch {
      // The server or AbortController may already have closed the stream.
    }
    reader.releaseLock()
  }

  if (!result) {
    throw new GatewayClientError(
      "gateway.incomplete_stream",
      "Kết nối kết thúc trước khi có kết quả hoàn chỉnh.",
      { retryable: true },
    )
  }
  return result
}

export async function parseHttpError(
  response: Response,
): Promise<GatewayClientError> {
  try {
    const payload: unknown = await response.json()
    const parsed = gatewayErrorResponseSchema.safeParse(payload)
    if (parsed.success) {
      return new GatewayClientError(
        parsed.data.error.code,
        parsed.data.error.message,
        {
          retryable: parsed.data.error.retryable,
          requestId: parsed.data.error.request_id,
          traceId: parsed.data.error.trace_id,
          httpStatus: response.status,
        },
      )
    }
  } catch {
    // Replace malformed or non-JSON server errors with a stable safe message.
  }
  return new GatewayClientError(
    `gateway.http_${response.status}`,
    GENERIC_ERROR_MESSAGE,
    { httpStatus: response.status },
  )
}

export function isAbortError(error: unknown): boolean {
  return (
    error instanceof DOMException && error.name === "AbortError"
  ) || (typeof error === "object" && error !== null && "name" in error && error.name === "AbortError")
}

function protocolError(code: string): GatewayClientError {
  return new GatewayClientError(code, "Phản hồi từ Gateway không đúng hợp đồng.")
}

function streamError(detail: GatewayErrorDetail): GatewayClientError {
  return new GatewayClientError(detail.code, detail.message, {
    retryable: detail.retryable,
    requestId: detail.request_id,
    traceId: detail.trace_id,
  })
}

function assertCorrelation(
  field: "request_id" | "trace_id",
  expected: string | undefined,
  received: string,
): string {
  if (expected !== undefined && received !== expected) {
    throw protocolError(`gateway.mismatched_${field}`)
  }
  return received
}
