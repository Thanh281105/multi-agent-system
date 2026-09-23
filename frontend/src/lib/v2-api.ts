import {
  wireActionConfirmRequestSchema,
  actionDecisionResponseSchema,
  actionExecutionResponseSchema,
  actionReadResponseSchema,
  wireActionRejectRequestSchema,
  wireChatRequestSchema,
  chatResponseSchema,
  wireConversationCreateRequestSchema,
  conversationCreateResponseSchema,
  conversationDetailResponseSchema,
  conversationListResponseSchema,
  conversationModeSchema,
  meResponseSchema,
  wirePreferenceDeleteRequestSchema,
  preferenceListResponseSchema,
  wirePreferencePutRequestSchema,
  preferenceRecordSchema,
  safeErrorResponseSchema,
  turnResponseSchema,
  type ActionConfirmRequest,
  type ActionDecisionResponse,
  type ActionExecutionResponse,
  type ActionReadResponse,
  type ActionRejectRequest,
  type ChatRequest,
  type ChatResponse,
  type ConversationCreateResponse,
  type ConversationDetailResponse,
  type ConversationListResponse,
  type ConversationMode,
  type MeResponse,
  type PreferenceDeleteRequest,
  type PreferenceListResponse,
  type PreferencePutRequest,
  type PreferenceRecord,
  type SafeErrorDetail,
  type TurnResponse,
  type TurnSseTerminalEvent,
} from "@/lib/v2-contracts"
import {
  readV2TurnStream,
  type V2StreamCallbacks,
} from "@/lib/v2-stream"

const V2_API_ROOT = "/api/v2"
const GENERIC_ERROR_MESSAGE = "Không thể xử lý yêu cầu lúc này."

interface RuntimeSchema<T> {
  safeParse(value: unknown):
    | { success: true; data: T }
    | { success: false }
}

export interface V2ApiClientOptions {
  baseUrl?: string
  fetchImpl?: typeof fetch
}

export interface V2RequestContext {
  apiKey: string
  signal?: AbortSignal
}

export interface V2CreateConversationOptions extends V2RequestContext {
  mode: ConversationMode
}

export interface V2ListConversationsOptions extends V2RequestContext {
  mode: ConversationMode
  limit?: number
}

export interface V2ConversationOptions extends V2RequestContext {
  conversationId: string
  limit?: number
}

export interface V2ChatOptions extends V2RequestContext {
  request: ChatRequest
}

export interface V2StreamChatOptions extends V2ChatOptions, V2StreamCallbacks {}

export interface V2TurnOptions extends V2RequestContext {
  turnId: string
}

export interface V2ActionOptions extends V2RequestContext {
  actionId: string
}

export interface V2ConfirmActionOptions extends V2ActionOptions {
  request: ActionConfirmRequest
  idempotencyKey: string
}

export interface V2RejectActionOptions extends V2ActionOptions {
  request: ActionRejectRequest
}

export interface V2ListPreferencesOptions extends V2RequestContext {
  mode: ConversationMode
}

export interface V2PutPreferenceOptions extends V2RequestContext {
  request: PreferencePutRequest
}

export interface V2DeletePreferenceOptions extends V2RequestContext {
  mode: ConversationMode
  request: PreferenceDeleteRequest
}

export interface V2ChatStreamRequest extends V2RequestContext {
  baseUrl?: string
  request: ChatRequest
}

interface V2ApiErrorOptions {
  source?: "server" | "client"
  retryable?: boolean
  requestId?: string
  traceId?: string
  httpStatus?: number
  validationErrors?: SafeErrorDetail["validationErrors"]
}

export class V2ApiError extends Error {
  readonly source: "server" | "client"
  readonly code: string
  readonly retryable: boolean
  readonly requestId?: string
  readonly traceId?: string
  readonly httpStatus?: number
  readonly validationErrors: SafeErrorDetail["validationErrors"]

  constructor(code: string, message: string, options: V2ApiErrorOptions = {}) {
    super(message)
    this.name = "V2ApiError"
    this.source = options.source ?? "client"
    this.code = code
    this.retryable = options.retryable ?? false
    this.requestId = options.requestId
    this.traceId = options.traceId
    this.httpStatus = options.httpStatus
    this.validationErrors = options.validationErrors ?? []
  }
}

export interface V2ApiClient {
  getMe(options: V2RequestContext): Promise<MeResponse>
  createConversation(options: V2CreateConversationOptions): Promise<ConversationCreateResponse>
  listConversations(options: V2ListConversationsOptions): Promise<ConversationListResponse>
  getConversation(options: V2ConversationOptions): Promise<ConversationDetailResponse>
  deleteConversation(options: V2ConversationOptions): Promise<void>
  chat(options: V2ChatOptions): Promise<ChatResponse>
  streamChat(options: V2StreamChatOptions): Promise<TurnSseTerminalEvent>
  getTurn(options: V2TurnOptions): Promise<TurnResponse>
  cancelTurn(options: V2TurnOptions): Promise<TurnResponse>
  getAction(options: V2ActionOptions): Promise<ActionReadResponse>
  confirmAction(options: V2ConfirmActionOptions): Promise<ActionExecutionResponse>
  rejectAction(options: V2RejectActionOptions): Promise<ActionDecisionResponse>
  listPreferences(options: V2ListPreferencesOptions): Promise<PreferenceListResponse>
  putPreference(options: V2PutPreferenceOptions): Promise<PreferenceRecord>
  deletePreference(options: V2DeletePreferenceOptions): Promise<void>
}

export function createV2ApiClient(options: V2ApiClientOptions = {}): V2ApiClient {
  const fetchImpl = options.fetchImpl ?? fetch
  const baseUrl = normalizeBaseUrl(options.baseUrl)

  const json = <T>(
    path: string,
    context: V2RequestContext,
    schema: RuntimeSchema<T>,
    init: RequestInit = {},
  ) => requestJson(fetchImpl, `${baseUrl}${V2_API_ROOT}${path}`, context, schema, init)

  const empty = (
    path: string,
    context: V2RequestContext,
    init: RequestInit,
  ) => requestEmpty(fetchImpl, `${baseUrl}${V2_API_ROOT}${path}`, context, init)

  return {
    getMe: (context) => json("/me", context, meResponseSchema),

    createConversation: async (requestOptions) => {
      const body = parseRequest(wireConversationCreateRequestSchema, {
        mode: requestOptions.mode,
      })
      return json("/conversations", requestOptions, conversationCreateResponseSchema, {
        method: "POST",
        body: JSON.stringify(body),
      })
    },

    listConversations: async (requestOptions) => {
      const mode = parseMode(requestOptions.mode)
      const query = queryString({
        mode,
        ...(requestOptions.limit === undefined
          ? {}
          : { limit: validateLimit(requestOptions.limit) }),
      })
      return json(`/conversations?${query}`, requestOptions, conversationListResponseSchema)
    },

    getConversation: async (requestOptions) => {
      const query = requestOptions.limit === undefined
        ? ""
        : `?${queryString({ limit: validateLimit(requestOptions.limit) })}`
      return json(
        `/conversations/${encodePath(requestOptions.conversationId)}${query}`,
        requestOptions,
        conversationDetailResponseSchema,
      )
    },

    deleteConversation: (requestOptions) => empty(
      `/conversations/${encodePath(requestOptions.conversationId)}`,
      requestOptions,
      { method: "DELETE" },
    ),

    chat: async (requestOptions) => {
      const body = parseRequest(wireChatRequestSchema, serializeChatRequest(requestOptions.request))
      return json("/chat", requestOptions, chatResponseSchema, {
        method: "POST",
        body: JSON.stringify(body),
      })
    },

    streamChat: async (requestOptions) => {
      const { url, init } = buildV2ChatStreamRequest({
        baseUrl,
        apiKey: requestOptions.apiKey,
        signal: requestOptions.signal,
        request: requestOptions.request,
      })
      const response = await safeFetch(fetchImpl, url, init)
      if (!response.ok) throw await parseV2HttpError(response)
      return readV2TurnStream(response, {
        onEvent: requestOptions.onEvent,
        onProgress: requestOptions.onProgress,
        onTextDelta: requestOptions.onTextDelta,
        onTerminal: requestOptions.onTerminal,
      })
    },

    getTurn: (requestOptions) => json(
      `/turns/${encodePath(requestOptions.turnId)}`,
      requestOptions,
      turnResponseSchema,
    ),

    cancelTurn: (requestOptions) => json(
      `/turns/${encodePath(requestOptions.turnId)}/cancel`,
      requestOptions,
      turnResponseSchema,
      { method: "POST" },
    ),

    getAction: (requestOptions) => json(
      `/actions/${encodePath(requestOptions.actionId)}`,
      requestOptions,
      actionReadResponseSchema,
    ),

    confirmAction: async (requestOptions) => {
      validateIdempotencyKey(requestOptions.idempotencyKey)
      const body = parseRequest(
        wireActionConfirmRequestSchema,
        { proposal_version: requestOptions.request.proposalVersion },
      )
      return json(
        `/actions/${encodePath(requestOptions.actionId)}/confirm`,
        requestOptions,
        actionExecutionResponseSchema,
        {
          method: "POST",
          body: JSON.stringify(body),
          headers: { "Idempotency-Key": requestOptions.idempotencyKey },
        },
      )
    },

    rejectAction: async (requestOptions) => {
      const body = parseRequest(wireActionRejectRequestSchema, {
        proposal_version: requestOptions.request.proposalVersion,
        ...(requestOptions.request.reason === undefined
          ? {}
          : { reason: requestOptions.request.reason }),
      })
      return json(
        `/actions/${encodePath(requestOptions.actionId)}/reject`,
        requestOptions,
        actionDecisionResponseSchema,
        { method: "POST", body: JSON.stringify(body) },
      )
    },

    listPreferences: async (requestOptions) => json(
      `/memory?${queryString({ mode: parseMode(requestOptions.mode) })}`,
      requestOptions,
      preferenceListResponseSchema,
    ),

    putPreference: async (requestOptions) => {
      const body = parseRequest(wirePreferencePutRequestSchema, {
        source_turn_id: requestOptions.request.sourceTurnId,
        preference: requestOptions.request.preference,
      })
      return json("/memory", requestOptions, preferenceRecordSchema, {
        method: "PUT",
        body: JSON.stringify(body),
      })
    },

    deletePreference: async (requestOptions) => {
      const body = parseRequest(wirePreferenceDeleteRequestSchema, {
        preference_id: requestOptions.request.preferenceId,
      })
      return empty(
        `/memory?${queryString({ mode: parseMode(requestOptions.mode) })}`,
        requestOptions,
        { method: "DELETE", body: JSON.stringify(body) },
      )
    },
  }
}

export function buildV2ChatStreamRequest(request: V2ChatStreamRequest): {
  url: string
  init: RequestInit
} {
  const body = parseRequest(wireChatRequestSchema, serializeChatRequest(request.request))
  return {
    url: `${normalizeBaseUrl(request.baseUrl)}${V2_API_ROOT}/chat/stream`,
    init: authenticatedInit(request, {
      method: "POST",
      headers: { Accept: "text/event-stream" },
      body: JSON.stringify(body),
    }),
  }
}

export async function parseV2HttpError(response: Response): Promise<V2ApiError> {
  try {
    const payload: unknown = await response.json()
    const parsed = safeErrorResponseSchema.safeParse(payload)
    if (parsed.success) {
      return new V2ApiError(parsed.data.error.code, parsed.data.error.message, {
        source: "server",
        retryable: parsed.data.error.retryable,
        requestId: parsed.data.error.requestId,
        traceId: parsed.data.error.traceId,
        httpStatus: response.status,
        validationErrors: parsed.data.error.validationErrors,
      })
    }
  } catch {
    // Malformed server responses are replaced with a stable, non-sensitive error.
  }
  return new V2ApiError(`v2.http_${response.status}`, GENERIC_ERROR_MESSAGE, {
    httpStatus: response.status,
  })
}

export function isAbortError(error: unknown): boolean {
  return (error instanceof DOMException && error.name === "AbortError") ||
    (typeof error === "object" && error !== null && "name" in error &&
      error.name === "AbortError")
}

async function requestJson<T>(
  fetchImpl: typeof fetch,
  url: string,
  context: V2RequestContext,
  schema: RuntimeSchema<T>,
  init: RequestInit,
): Promise<T> {
  const response = await safeFetch(fetchImpl, url, authenticatedInit(context, init))
  if (!response.ok) throw await parseV2HttpError(response)

  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new V2ApiError("v2.invalid_json_response", GENERIC_ERROR_MESSAGE, {
      httpStatus: response.status,
    })
  }
  const parsed = schema.safeParse(payload)
  if (!parsed.success) {
    throw new V2ApiError("v2.invalid_response", GENERIC_ERROR_MESSAGE, {
      httpStatus: response.status,
    })
  }
  return parsed.data
}

async function requestEmpty(
  fetchImpl: typeof fetch,
  url: string,
  context: V2RequestContext,
  init: RequestInit,
): Promise<void> {
  const response = await safeFetch(fetchImpl, url, authenticatedInit(context, init))
  if (!response.ok) throw await parseV2HttpError(response)
  if (response.status !== 204) {
    throw new V2ApiError("v2.invalid_empty_response", GENERIC_ERROR_MESSAGE, {
      httpStatus: response.status,
    })
  }
}

async function safeFetch(
  fetchImpl: typeof fetch,
  url: string,
  init: RequestInit,
): Promise<Response> {
  try {
    return await fetchImpl(url, init)
  } catch (error) {
    if (isAbortError(error)) throw error
    throw new V2ApiError(
      "v2.network_error",
      "Không thể kết nối đến API v2.",
      { retryable: true },
    )
  }
}

function authenticatedInit(
  context: V2RequestContext,
  init: RequestInit,
): RequestInit {
  const headers = new Headers(init.headers)
  headers.set("Accept", headers.get("Accept") ?? "application/json")
  headers.set("X-API-Key", context.apiKey)
  if (init.body !== undefined) headers.set("Content-Type", "application/json")
  return {
    ...init,
    headers,
    signal: context.signal,
    credentials: "same-origin",
    cache: "no-store",
  }
}

function parseRequest<T>(schema: RuntimeSchema<T>, value: unknown): T {
  const parsed = schema.safeParse(value)
  if (!parsed.success) {
    throw new V2ApiError("v2.invalid_request", "Yêu cầu v2 không hợp lệ.")
  }
  return parsed.data
}

function normalizeBaseUrl(baseUrl?: string): string {
  return (baseUrl ?? "").replace(/\/+$/, "")
}

function parseMode(mode: ConversationMode): ConversationMode {
  return parseRequest(conversationModeSchema, mode)
}

function validateLimit(limit: number): number {
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new V2ApiError("v2.invalid_request", "Giới hạn phải từ 1 đến 100.")
  }
  return limit
}

function validateIdempotencyKey(value: string): void {
  if (value.length < 8 || value.length > 128 || !/^[!-~]+$/.test(value)) {
    throw new V2ApiError(
      "v2.invalid_idempotency_key",
      "Khóa chống lặp không hợp lệ.",
    )
  }
}

function serializeChatRequest(request: ChatRequest): {
  conversation_id: string
  client_turn_id: string
  message: string
} {
  return {
    conversation_id: request.conversationId,
    client_turn_id: request.clientTurnId,
    message: request.message,
  }
}

function encodePath(value: string): string {
  return encodeURIComponent(value)
}

function queryString(values: Record<string, string | number>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) query.set(key, String(value))
  return query.toString()
}
