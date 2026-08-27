# API v1 và SSE contract

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

## 2. Chat JSON

### `POST /api/v1/chat`

Request:

```json
{
  "message": "Tìm tai nghe dưới 1 triệu, bán tốt và ít bị khách phàn nàn.",
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
  "answer": "Đã so sánh ... theo dữ liệu mẫu ...",
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
      "source_type": "sample.database",
      "source_id": "postgresql:products",
      "fields": ["price", "rating", "sold_count", "shop", "platform"],
      "sample_data": true,
      "observed_at": "2026-08-24T00:00:00Z"
    }
  ],
  "warnings": [],
  "sample_data": true,
  "duration_ms": 18.5
}
```

`executions` chỉ công khai metadata an toàn; không có prompt, chain-of-thought,
raw tool arguments hoặc raw tool payload.

## 3. Chat streaming

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
completed: GatewayChatResponse
```

Khi lỗi sau lúc stream đã mở, terminal event là `error` chứa cùng error envelope
v1. Idle stream gửi comment `: heartbeat` mỗi 10 giây. Mỗi status event có
`sequence`, correlation IDs và, nếu có, step/agent/status. Server gửi đúng một
terminal event; client nên ngừng đọc ngay sau `completed` hoặc `error`.

Ví dụ:

```text
id: req_abc:1
retry: 3000
event: status
data: {"sequence":1,"phase":"request.accepted",...}

event: completed
data: {"api_version":"v1",...}
```

## 4. Error envelope

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

## 5. Health và operations

| Endpoint | Auth | Ý nghĩa |
| --- | --- | --- |
| `GET /health`, `/livez` | Không | Process sống; không kiểm tra dependency |
| `GET /readyz` | Không | Runtime + production schema revision + configured Redis/Qdrant contract |
| `GET /metrics` | Operations key ở production | Prometheus text format |
| `GET /api/v1/operations/traces/{trace_id}` | Operations key | Redacted bounded trace events |
| `GET /api/v1/operations/audit?limit=50` | Operations key | Agent Gateway audit, limit 1..200 |
| `GET /api/v1/operations/agents` | Operations key | Registry inventory/capabilities |

Không dùng `/livez` để route traffic. Load balancer readiness phải gọi
`/readyz`; 503 có body nêu dependency nào failed nhưng không lộ credential.
Ở production, check database yêu cầu đúng Alembic revision hiện hành; check
Qdrant yêu cầu service sống, collection đúng vector size/distance và có ít nhất
một knowledge point. Development/test giữ DB check ở mức round-trip để hỗ trợ
schema fixture cô lập.

## 6. Legacy API

`POST /chat` là đường Phase 1 single-agent tương thích ngược. Nó chỉ được mount
khi `LEGACY_CHAT_ENABLED=true` và production validation bắt buộc giá trị này là
`false`. Client mới phải dùng `/api/v1`.

## 7. Browser client

Frontend same-origin ở `/` gọi SSE bằng `fetch` để gửi custom API-key header.
Key chỉ nằm trong JavaScript memory đến khi reload, không lưu vào localStorage
hay sessionStorage. Session ID có thể lưu trong sessionStorage vì không phải
credential. Dữ liệu server được render bằng `textContent`, không dùng
`innerHTML`.
