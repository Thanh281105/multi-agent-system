# API v1, v2 và SSE contracts

API v1 là compatibility boundary cho historical Product/Review/Trust/Market
runtime. API v2 là durable boundary cho conversation, turn, action, memory và
knowledge evidence; nó dùng PostgreSQL làm authority và yêu cầu published
corpus/index binding. V1 và v2 không dùng chung lifecycle hay response model.

## 1. Authentication và correlation

Chat endpoints yêu cầu header:

```http
X-API-Key: <secret>
```

`GATEWAY_API_KEYS` dùng format `principal:secret[,principal:secret]`; principal
không được gửi từ client. Gateway ánh xạ secret sang principal bằng
constant-time comparison, sau đó áp dụng per-principal rate limit.

Quyền không suy ra từ `principal_id`. `GATEWAY_PRINCIPAL_POLICIES` phải khai báo
policy tin cậy theo format `principal:tenant:scope1|scope2`; principal đã xác
thực nhưng chưa có policy bị từ chối với HTTP 403. Tenant và scope trong policy
được truyền nguyên vẹn qua A2A tới Agent Gateway để kiểm tra `required_user_scope`.

Client có thể gửi `X-Request-ID` theo pattern
`req_[a-zA-Z0-9_-]{3,120}`. Giá trị sai pattern bị thay bằng ID server sinh.
`X-Trace-ID` luôn do server sinh. Cả hai được trả ở response headers và body.

Operations endpoints dùng credential tách biệt:

```http
X-Operations-Key: <operations-secret>
```

## 2. API v2 durable

V2 dùng cùng `X-API-Key` và server-derived principal/tenant/scope. Client chỉ
chọn `mode` (`shopper` hoặc `merchant`); identity, store, catalog version,
corpus version và index manifest do server bind. Runtime phải resolve
`books-v1-calibrated-20260909` (hoặc một snapshot đã publish tương thích) từ
PostgreSQL trước khi nhận turn. Thiếu `V2_CORPUS_VERSION_ID`, lệch
`V2_INDEX_MANIFEST_ID`, database không phải PostgreSQL, index chưa complete hoặc
embedding model/dimension không khớp đều làm v2 fail closed.

### Endpoint inventory

| Method | Path | Semantics |
| --- | --- | --- |
| `GET` | `/api/v2/me` | Identity và allowed modes |
| `POST` | `/api/v2/conversations` | Tạo owner-bound conversation |
| `GET` | `/api/v2/conversations?mode=...` | Liệt kê conversation |
| `GET` / `DELETE` | `/api/v2/conversations/{conversation_id}` | Đọc hoặc xóa conversation/history |
| `POST` | `/api/v2/chat` | Admit/claim/reuse durable turn và trả `TurnResponse` |
| `POST` | `/api/v2/chat/stream` | Durable turn qua SSE |
| `GET` / `POST` | `/api/v2/turns/{turn_id}` / `/cancel` | Đọc hoặc cancel turn |
| `GET` | `/api/v2/actions/{action_id}` | Đọc action/proposal result |
| `POST` | `/api/v2/actions/{action_id}/confirm` | Confirm bằng `Idempotency-Key` |
| `POST` | `/api/v2/actions/{action_id}/reject` | Reject proposal |
| `GET` / `PUT` / `DELETE` | `/api/v2/memory` | Đọc, ghi hoặc xóa explicit preference |

Tạo conversation:

```http
POST /api/v2/conversations
X-API-Key: <secret>
Content-Type: application/json

{"mode":"shopper"}
```

Chat yêu cầu `conversation_id`, client-owned `client_turn_id` và message tối đa
2.000 ký tự:

```http
POST /api/v2/chat
X-API-Key: <secret>
Content-Type: application/json

{"conversation_id":"conversation_...","client_turn_id":"turn-001","message":"Tìm sách về lịch sử."}
```

V2 lưu trạng thái `pending → running → completed|failed|cancelled|interrupted`,
giữ operation/evidence/usage metadata và trả lại kết quả đã settle khi client
retry cùng identity. Không gửi raw prompt, chain-of-thought, tool payload hay
credential trong response. `Idempotency-Key` bắt buộc cho confirm action; scope,
role và confirmation state được kiểm tra từ PostgreSQL.

V2 SSE sử dụng các event contract `progress`, `text_delta` và một terminal event.
Passive disconnect/timeout không được hiểu là server đã settle turn; client đọc
`GET /api/v2/turns/{turn_id}` để reattach/recover.

## 3. Chat JSON (API v1)

### `POST /api/v1/chat`

Request:

```json
{
  "message": "Gợi ý sách dưới 150.000 đồng, rating tốt và ít tín hiệu phàn nàn.",
  "session_id": null
}
```

- `message`: sau trim từ 1 đến 2.000 ký tự;
- `session_id`: bỏ qua ở turn đầu; follow-up dùng ID response, pattern
  `sess_[a-zA-Z0-9_-]{3,120}`;
- extra fields bị từ chối.

Response rút gọn:

```json
{
  "api_version": "v1",
  "status": "success",
  "answer": "Đã so sánh ... theo snapshot lịch sử Tiki Books ...",
  "session_id": "sess_...",
  "request_id": "req_...",
  "trace_id": "trace_...",
  "intent": "multi.recommendation",
  "active_agent": "product_agent",
  "selected_product_id": 2,
  "executions": [
    {
      "step_id": "step_product",
      "agent_id": "product_agent",
      "action": "product.rank",
      "status": "success",
      "duration_ms": 4.2,
      "error_codes": []
    }
  ],
  "provenance": [
    {
      "source_type": "sample.public_dataset",
      "source_id": "tiki-books:kaggle-v4:eval",
      "fields": [
        "name", "authors", "publisher", "category", "page_count",
        "price", "rating", "sold_count"
      ],
      "sample_data": true,
      "observed_at": "2026-08-30T12:34:56Z"
    }
  ],
  "warnings": [],
  "sample_data": true,
  "duration_ms": 18.5
}
```

`executions` chỉ công khai metadata an toàn; không có prompt, chain-of-thought,
raw tool arguments hoặc raw tool payload.

## 4. Chat streaming (API v1)

### `POST /api/v1/chat/stream`

Request body/auth giống JSON endpoint. Response media type
`text/event-stream`, `Cache-Control: no-cache, no-transform` và
`X-Accel-Buffering: no`.

Event lifecycle:

```text
status: request.accepted
status: routing.completed
status: planning.completed
status: agent.started / agent.completed (0..n)
status: aggregation.completed
token: {"sequence":...,"delta":"...","request_id":"...","trace_id":"..."} (0..n)
completed: GatewayChatResponse
```

Khi lỗi sau lúc stream đã mở, terminal event là `error` chứa cùng error envelope
v1. Idle stream gửi comment `: heartbeat` mỗi 10 giây. Mỗi status event có
`sequence`, correlation IDs và, nếu có, step/agent/status. Server gửi đúng một
terminal event; client nên ngừng đọc ngay sau `completed` hoặc `error`. Các
`token` event mang delta nhỏ của câu trả lời đã được grounded, được phát sau
khi orchestration hoàn tất để giao diện hiển thị dần mà không tạo thêm nội dung
ngoài bằng chứng.

Ví dụ:

```text
id: req_abc:1
retry: 3000
event: status
data: {"sequence":1,"phase":"request.accepted",...}

event: token
data: {"sequence":8,"delta":"Tai ","request_id":"req_abc",...}

event: completed
data: {"api_version":"v1",...}
```

## 5. Error envelope

```json
{
  "error": {
    "code": "gateway.validation_failed",
    "message": "Payload không hợp lệ.",
    "request_id": "req_...",
    "trace_id": "trace_...",
    "retryable": false,
    "validation_errors": [
      {
        "field": "body.message",
        "type": "string_too_long",
        "message": "String should have at most 2000 characters"
      }
    ]
  }
}
```

| HTTP | Code điển hình | Retry |
| ---: | --- | --- |
| 401 | `gateway.authentication_failed` | Không, sửa credential |
| 403 | `gateway.authorization_not_configured` | Không, cấp policy cho principal |
| 404 | `gateway.session_not_found` | Không, tạo session mới |
| 409 | `gateway.session_busy` | Có, backoff |
| 422 | `gateway.validation_failed` | Không, sửa payload |
| 429 | `gateway.authentication_rate_limited`, `gateway.rate_limited` | Có, theo `Retry-After` |
| 500 | `gateway.internal_error` | Có, dùng correlation để tra log |
| 503 | `gateway.shared_state_unavailable`, `gateway.all_agents_failed` | Có |
| 504 | `gateway.orchestration_timeout` | Có |

401 có `WWW-Authenticate: ApiKey`. Rate-limited response có
`X-RateLimit-Limit`, `X-RateLimit-Remaining` và khi cần `Retry-After`.

## 6. Health và operations

| Endpoint | Auth | Ý nghĩa |
| --- | --- | --- |
| `GET /health`, `/livez` | Không | Process sống; không kiểm tra dependency |
| `GET /readyz` | Không | Runtime + `20260910_0008` schema + Redis nếu cấu hình + v1/v2 knowledge contract |
| `GET /metrics` | Operations key ở production | Prometheus text format |
| `GET /api/v1/operations/traces/{trace_id}` | Operations key | Redacted bounded trace events |
| `GET /api/v1/operations/audit?limit=50` | Operations key | Agent Gateway audit, limit 1..200 |
| `GET /api/v1/operations/agents` | Operations key | Registry inventory/capabilities |

Không dùng `/livez` để route traffic. Load balancer readiness phải gọi
`/readyz`; 503 có body nêu dependency nào failed nhưng không lộ credential.
Ở production, check database yêu cầu đúng Alembic revision hiện hành
`20260910_0008`. V1 mặc định `KNOWLEDGE_BACKEND=disabled`, vì vậy response v1
có `knowledge: "disabled"` và không cần Qdrant. V2 readiness/resolve phải thấy
published PostgreSQL corpus/index, gồm đúng 20 vectors cho
`books-v1-calibrated-20260909`, và kiểm tra embedding contract trước retrieval.
Development/test có thể giữ DB check round-trip khi chỉ chạy fixture, nhưng
không được gọi đó là v2 durable verification.

## 7. Legacy API

`POST /chat` là đường Phase 1 single-agent tương thích ngược. Nó chỉ được mount
khi `LEGACY_CHAT_ENABLED=true` và production validation bắt buộc giá trị này là
`false`. Legacy runner dùng `store=false`; follow-up dựa trên transcript ngắn
hạn trong memory của process, không dùng `previous_response_id`. Client mới phải
dùng `/api/v1`; client cần durable conversation/turn/action dùng `/api/v2`.

## 8. Browser client

Frontend same-origin ở `/` gọi SSE bằng `fetch` để gửi custom API-key header.
Key chỉ nằm trong JavaScript memory đến khi reload, không lưu vào localStorage
hay sessionStorage. Session ID có thể lưu trong sessionStorage vì không phải
credential. Dữ liệu server được render bằng `textContent`, không dùng
`innerHTML`.
