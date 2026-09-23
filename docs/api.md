# API v2 và SSE contracts

`/api/v2` là API HTTP có version duy nhất. Các endpoint `/api/v1` đã bị gỡ;
client mới dùng contract hội thoại bền vững V2. Health endpoints vẫn ở root,
còn `POST /chat` là fixture Phase 1 riêng, chỉ được mount khi bật cờ cấu hình.

## 1. Authentication và correlation

Các endpoint chat yêu cầu header:

```http
X-API-Key: <secret>
```

`GATEWAY_API_KEYS` dùng format `principal:secret[,principal:secret]`; principal
không được gửi từ client. Gateway ánh xạ secret sang principal bằng
constant-time comparison, sau đó áp dụng per-principal rate limit.

Quyền không suy ra từ `principal_id`. `GATEWAY_PRINCIPAL_POLICIES` phải khai báo
policy tin cậy theo format `principal:tenant:scope1|scope2`; principal đã xác
thực nhưng chưa có policy bị từ chối với HTTP 403. Tenant và scope trong policy
được truyền qua Agent Gateway để kiểm tra quyền.

Client có thể gửi `X-Request-ID` theo pattern
`req_[a-zA-Z0-9_-]{3,120}`. Giá trị sai pattern bị thay bằng ID server sinh.
`X-Trace-ID` luôn do server sinh. Cả hai được trả ở response headers và trong
các response V2 có correlation envelope.

Operations endpoints dùng credential tách biệt:

```http
X-Operations-Key: <operations-secret>
```

## 2. API v2 durable

Client chỉ chọn `mode` (`shopper` hoặc `merchant`); identity, store, catalog
version, corpus version và index manifest do server bind. PostgreSQL là nguồn
chính cho hội thoại, turn, action và published knowledge. Cấu hình runtime dùng
`V2_CORPUS_VERSION_ID` và tùy chọn `V2_INDEX_MANIFEST_ID`; Docker Compose truyền
hai biến này từ môi trường vào app container.

V2 chỉ nhận turn khi resolve được corpus/index đã publish trong PostgreSQL và
embedding model/dimension khớp. Thiếu binding hoặc snapshot hợp lệ làm runtime
fail closed với HTTP 503 (`v2.runtime_unavailable`). V2 không dùng Qdrant.

### Endpoint inventory

| Method | Path | Semantics |
| --- | --- | --- |
| `GET` | `/api/v2/me` | Identity và allowed modes |
| `POST` | `/api/v2/conversations` | Tạo owner-bound conversation |
| `GET` | `/api/v2/conversations?mode=...` | Liệt kê conversation |
| `GET` / `DELETE` | `/api/v2/conversations/{conversation_id}` | Đọc hoặc xóa conversation/history |
| `POST` | `/api/v2/chat` | Admit/claim/reuse durable turn và trả `TurnResponse` |
| `POST` | `/api/v2/chat/stream` | Durable turn qua SSE |
| `GET` | `/api/v2/turns/{turn_id}` | Đọc trạng thái/result của turn |
| `POST` | `/api/v2/turns/{turn_id}/cancel` | Yêu cầu hủy turn |
| `GET` | `/api/v2/actions/{action_id}` | Đọc action/proposal result |
| `POST` | `/api/v2/actions/{action_id}/confirm` | Confirm bằng `Idempotency-Key` |
| `POST` | `/api/v2/actions/{action_id}/reject` | Reject proposal |
| `GET` / `PUT` / `DELETE` | `/api/v2/memory` | Đọc, ghi hoặc xóa explicit preference |

### Tạo conversation và gửi chat

Tạo conversation trước khi gửi turn:

```http
POST /api/v2/conversations
X-API-Key: <secret>
Content-Type: application/json

{"mode":"shopper"}
```

Lấy `conversation.conversation_id` từ response. Mỗi lần gửi cần một
`client_turn_id` mới do client tạo; retry cùng một turn phải dùng lại ID đó.

```http
POST /api/v2/chat
X-API-Key: <secret>
Content-Type: application/json

{"conversation_id":"conversation_...","client_turn_id":"browser:turn-001","message":"Tìm sách về lịch sử."}
```

Request có đúng ba trường: `conversation_id`, `client_turn_id` và `message` tối
đa 2.000 ký tự. Message rỗng hoặc chỉ có khoảng trắng, ID sai pattern và extra
fields bị từ chối. Response là `TurnResponse`; schema đầy đủ được công bố trong
OpenAPI tại `/openapi.json`.

V2 lưu trạng thái `pending → running → completed|failed|cancelled|interrupted`,
giữ operation/evidence/usage metadata và trả lại kết quả đã settle khi client
retry cùng identity. `Idempotency-Key` bắt buộc cho confirm action; scope, role
và confirmation state được kiểm tra từ PostgreSQL.

### SSE

`POST /api/v2/chat/stream` nhận cùng request/auth với JSON chat và trả
`text/event-stream`. Event lifecycle dùng `progress`, `text_delta` và đúng một
event `terminal`. Terminal chứa kết quả cuối cùng và `server_settled`.
Disconnect hoặc timeout phía client không có nghĩa server đã settle turn; dùng
`GET /api/v2/turns/{turn_id}` để đọc lại trạng thái và khôi phục.

## 3. Error envelope

API V2 trả lỗi an toàn theo dạng:

```json
{
  "error": {
    "code": "v2.validation_failed",
    "message": "Payload không hợp lệ.",
    "request_id": "req_...",
    "trace_id": "trace_...",
    "retryable": false,
    "validation_errors": [
      {
        "field": "body.message",
        "type": "value_error",
        "message": "Value error, message must contain non-whitespace text"
      }
    ]
  }
}
```

Các lỗi không tiết lộ prompt, chain-of-thought, tool payload hay credential.
Response 401 có `WWW-Authenticate: ApiKey`; rate limit trả các header
`X-RateLimit-Limit`, `X-RateLimit-Remaining` và khi cần `Retry-After`.

## 4. Health và operations

| Endpoint | Auth | Ý nghĩa |
| --- | --- | --- |
| `GET /health`, `/livez` | Không | Process sống; không kiểm tra dependency |
| `GET /readyz` | Không | Runtime, PostgreSQL schema và Redis nếu cấu hình |
| `GET /metrics` | Operations key ở production | Prometheus text format |
| `GET /api/v2/operations/traces/{trace_id}` | Operations key | Redacted bounded trace events |
| `GET /api/v2/operations/audit?limit=50` | Operations key | Agent Gateway audit, limit 1..200 |
| `GET /api/v2/operations/agents` | Operations key | Registry inventory/capabilities |

Không dùng `/livez` để route traffic. Load balancer readiness gọi `/readyz`;
503 nêu dependency nào failed nhưng không lộ credential. Ở production, check
database yêu cầu Alembic revision `20260910_0008`. Readiness không thay thế việc
resolve binding V2: request cần corpus/index PostgreSQL đã publish và khớp
embedding contract trước retrieval.

## 5. Fixture Phase 1

`POST /chat` là endpoint không version dành cho fixture single-agent Phase 1. Nó
chỉ được mount khi `LEGACY_CHAT_ENABLED=true`; production validation bắt buộc
giá trị này là `false`. Endpoint này không phải alias của API V2. Client ứng dụng
và tích hợp mới dùng `/api/v2`.

## 6. Browser client

Frontend same-origin gọi `/api/v2/chat/stream` bằng `fetch` để gửi custom API-key
header. Key chỉ nằm trong JavaScript memory đến khi reload, không lưu vào
localStorage hay sessionStorage. Metadata cần khôi phục conversation/turn có thể
được lưu trong sessionStorage; server vẫn là authority cho history và trạng thái.
Dữ liệu server được render bằng `textContent`, không dùng `innerHTML`.
