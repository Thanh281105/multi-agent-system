# Kiến trúc hệ thống

## 1. Mục tiêu và phạm vi

Hệ thống hiện thực hóa workflow multi-agent dưới dạng **modular monolith**: các
boundary được biểu diễn bằng contract và adapter rõ ràng nhưng vẫn chạy trong
một process để phù hợp quy mô khóa luận, dữ liệu mẫu và khả năng vận hành của
một nhóm nhỏ. Cách tiếp cận này giữ được khả năng kiểm thử end-to-end và tránh
đưa độ phức tạp mạng/phân tán vào trước khi có tải thực tế.

Mục tiêu:

- trả lời tiếng Việt dựa trên facts/provenance thay vì tự tạo thông tin;
- điều phối Product, Review, Trust và Market domain độc lập;
- hỗ trợ partial success khi một miền phụ trợ lỗi;
- dùng cùng typed contracts cho in-process A2A và transport tương lai;
- tái lập được trên dữ liệu mẫu, kể cả evaluation và failure injection;
- fail closed với production secrets/state/knowledge không an toàn.

Không thuộc phạm vi hiện tại:

- marketplace crawler và dữ liệu người dùng thật;
- fine-tuning hoặc semantic embedding model production;
- long-term personalized memory cho preference, conversation summary và
  historical artifacts;
- microservice/service mesh/Kubernetes;
- multi-region HA, distributed tracing backend, distributed rate limiter;
- khẳng định chất lượng vượt single-agent khi chưa có frozen real-model baseline.

## 2. Deployment view

```mermaid
flowchart TB
    subgraph Edge
        RP[Reverse proxy TLS - deployment responsibility]
        API[FastAPI + static web client]
    end

    subgraph Application[One non-root read-only container]
        GW[HTTP Gateway]
        ORCH[Orchestrator]
        DOM[Four Domain Agents]
        AG[Agent Gateway / Registry]
        CAT[MCP-style Catalog]
        TEL[In-memory bounded telemetry]
    end

    subgraph Data[Internal Docker network]
        PG[(PostgreSQL)]
        REDIS[(Redis)]
        QD[(Qdrant)]
    end

    RP --> API --> GW --> ORCH --> DOM --> AG --> CAT
    CAT --> PG
    CAT --> QD
    ORCH <--> REDIS
    GW & ORCH & AG --> TEL
```

Compose chỉ publish backend vào `127.0.0.1` mặc định. Ba data services nằm trên
internal network. Backend chạy UID/GID `10001`, filesystem read-only, drop toàn
bộ Linux capabilities và bật `no-new-privileges`.

Một Uvicorn worker là lựa chọn có chủ đích vì inbound rate limiter, metric
aggregation và trace buffer hiện nằm trong process. Redis đã cung cấp shared
session/memory/turn-lock để bước scale-out sau không phải đổi public contract,
nhưng scale nhiều worker chỉ hợp lệ sau khi chuyển rate limit và telemetry sang
distributed backend.

## 3. Logical components

| Component | Input | Output | Failure boundary |
| --- | --- | --- | --- |
| Gateway | Authenticated HTTP payload | Stable JSON/SSE contract | 401/404/409/422/429/503/504 envelope |
| Intent Router | Message + session state | `RoutedIntent` | Unsupported intent calls no tools |
| Planner | `RoutedIntent` | Validated `ExecutionPlan` DAG | Dependency order validated by Pydantic |
| Executor | Plan + correlation context | Ordered `AgentResult` tuple | Exceptions sanitized to typed agent error |
| Aggregator | Intent + agent results | Grounded answer/status/warnings | Failed/partial/success deterministic |
| Agent Gateway | Agent request + registry policy | MCP tool response + audit | Agent/server/tool/permission allowlists |
| Repositories/tools | Validated arguments | JSON-friendly sample facts | Short DB sessions, rollback on exception |
| Shared state | Principal/session/memory | TTL-bound context | Redis outage becomes readiness/API 503 |
| Knowledge adapter | Sample note query | Qdrant matches + provenance | Bounded timeout/retry and contract checks |

## 4. Request lifecycle

```mermaid
sequenceDiagram
    participant C as Client
    participant G as Gateway
    participant O as Orchestrator
    participant D as Domain Agents
    participant A as Agent Gateway
    participant S as PostgreSQL/Qdrant
    participant R as Redis

    C->>G: POST /api/v1/chat[/stream] + X-API-Key
    G->>G: auth, peer/principal rate limit, correlation
    G->>R: validate owner-bound session / acquire turn lock
    G->>O: message + principal + session + trace
    O->>O: route intent, extract entities, build DAG
    par ready steps
        O->>D: typed AgentMessage
        D->>A: allowlisted tool call
        A->>S: bounded read
        S-->>A: structured sample facts
        A-->>D: GatewayResponse + redacted audit
        D-->>O: AgentResult + errors + provenance
    end
    O->>O: aggregate and score only returned facts
    O->>R: update state/memory with TTL
    O-->>G: OrchestrationResult
    G-->>C: response or terminal SSE event
```

Correlation IDs `request_id`, `trace_id`, `session_id`, `task_id`, `agent_id`
được truyền xuyên suốt. Access logs/trace attributes không chứa prompt, raw tool
arguments, API key hay nội dung review.

## 5. Agent-to-Agent contract

`app/contracts/a2a.py` định nghĩa immutable Pydantic models:

- `AgentMessage`: source, target, action, correlation IDs và typed payload;
- `AgentResult`: terminal status, structured data, safe errors, provenance và
  duration;
- `ExecutionStep`: agent/action/input/dependencies;
- `ExecutionPlan`: DAG có step ID duy nhất và chỉ phụ thuộc step đã xuất hiện;
- `TaskStatus`: `pending`, `running`, `success`, `partial_success`, `failed`.

Contract không chứa transport-specific fields. Khi tách agent thành service,
adapter HTTP/message-bus có thể serialize cùng schema; planner/aggregator không
cần đổi semantics.

## 6. Domain agents và DAG

| Agent | Actions chính | Sources |
| --- | --- | --- |
| Product | search, rank, compare, statistics | PostgreSQL products/shops |
| Review | retrieve, sentiment, aspect, summarize, compare batch | PostgreSQL reviews |
| Trust | complaint, trust heuristic, compare batch | PostgreSQL reviews |
| Market | category statistics, market-note retrieval | PostgreSQL + Qdrant |

Recommendation đa miền tạo ba step:

```text
product.rank
   ├── review.compare(product_ids từ toàn bộ ranking)
   └── trust.compare(product_ids từ toàn bộ ranking)
```

Hai nhánh phụ trợ có thể chạy đồng thời sau Product step. Executor giữ nguyên
thứ tự candidate và giới hạn tối đa 5 IDs.

## 7. Scoring minh bạch

Product ranking trong một candidate set:

```text
product_score = 0.45 × rating/5
              + 0.35 × log(1 + sold)/log(1 + max_sold)
              + 0.20 × (1 - price/max_price)
```

Recommendation score:

```text
0.55 × product_fit
+ 0.15 × positive_sentiment
+ 0.20 × (1 - complaint_rate)
+ 0.10 × review_trust
```

Nếu một auxiliary signal không phủ mọi candidate, signal đó bị bỏ cho **tất
cả** candidate và phần trọng số còn lại được normalize. Tiebreak ổn định theo
score, source rank, price và product ID. Response công khai method, score,
coverage và nhãn heuristic/sample; trọng số là product policy phiên bản 1, chưa
được hiệu chỉnh bằng dữ liệu marketplace thật.

## 8. Data và state

### PostgreSQL

Nguồn sự thật cho shops/products/reviews. Alembic quản lý schema. Sample seed:

- idempotent khi DB khớp chính xác snapshot;
- từ chối DB lẫn hoặc có dữ liệu ngoài snapshot;
- reset cần hai flags và bị chặn ở production.

### Redis

Lưu owner-bound session, active agent, last product, short-term memory và
distributed turn lock. Memory giữ tối đa 40 user/assistant turn entries mỗi
session, mỗi entry tối đa 2.000 ký tự; toàn bộ key hết hạn theo TTL mặc định
3.600 giây. Optimistic atomic update ngăn lost update; chỉ một turn được xử lý
trên một session tại một thời điểm.

Router deterministic v1 chỉ đọc structured session state (`active_agent`,
`last_product_id`); free-text memory được lưu cho lifecycle/audit mở rộng nhưng
chưa đưa vào routing hay aggregation. Khi bổ sung model runtime, cần projection
có giới hạn, chống prompt injection và policy xóa/đồng ý trước khi sử dụng phần
text này làm context.

### Qdrant

Lưu market notes mẫu. Embedding `hashed_token_cosine_v1` là vector hashing 128
chiều có version để tái lập, không phải semantic model. Seed job kiểm tra schema
trước upsert; production readiness kiểm tra lại vector size/distance và yêu cầu
collection có ít nhất một point. API key được gửi bằng header và không xuất
hiện trong public settings/logs.

## 9. Reliability semantics

- Liveness chỉ phản ánh process. Production readiness yêu cầu DB đúng Alembic
  revision hiện hành, Redis ping thành công và Qdrant sống với collection đúng
  vector contract, không rỗng; development/test chỉ ping dependency đã cấu hình.
- Mỗi orchestration có timeout cấu hình; timeout trả 504 retryable.
- Concurrent turn cùng session trả 409 retryable thay vì ghi đè state.
- Redis/Qdrant outage trả trạng thái failed/503 có error code ổn định.
- Một agent phụ trợ lỗi có thể tạo `partial_success`; answer chỉ dùng phần dữ
  liệu còn lại và luôn kèm warning.
- Tất cả agents lỗi trả gateway 503; không có fallback tạo facts.
- SSE luôn có `request.accepted`, progress có sequence và đúng một terminal
  `completed` hoặc `error`; heartbeat 10 giây giữ connection.

## 10. Security model

Trust boundaries:

1. Client → Gateway: API key, bounded body, auth-attempt limiter, per-principal
   limiter, constant-time compare.
2. Gateway → Orchestrator: principal/session ownership và correlation đã xác
   thực.
3. Agent → Agent Gateway: immutable registry, MCP server/tool/permission
   allowlists và outbound policy.
4. Application → Data: internal network, authenticated Redis/Qdrant/PostgreSQL.
5. Operator plane: `X-Operations-Key` tách khỏi user API key.

Production validation chặn demo/default/placeholder credentials, legacy route,
memory state và static knowledge. HTTP responses có CSP, frame denial, nosniff,
referrer/permissions policies, no-store cho API; docs/OpenAPI tắt ở production.
TLS termination, rotation, secret manager và firewall là trách nhiệm deployment.

## 11. Observability

- Access log: method, safe path label, status, request/trace IDs, latency.
- Trace ring: tối đa 5.000 redacted events/process.
- Metrics: HTTP/agent operation counters và aggregate duration theo bounded
  labels; tránh cardinality từ user path/ID.
- Operator endpoints: metrics, trace lookup, Agent Gateway audit và registry
  inventory, đều bảo vệ bằng operations key trong production.

Telemetry hiện không bền và không phân tán; production nhiều instance cần
Prometheus collector + OpenTelemetry/log backend bên ngoài.

## 12. Workflow coverage và evolution

| Workflow area | Trạng thái hiện tại |
| --- | --- |
| Single-agent baseline | Legacy code/tests còn giữ; real-model benchmark chưa đủ nên đánh dấu unavailable |
| Domain agents | Product, Review, Trust, Market đã triển khai |
| Orchestrator | Routing, planning, parallel DAG execution, aggregation, failure semantics |
| Shared platform | Session, Redis short-term turn storage, correlation, tracing, metrics, evaluation; chưa có long-term user memory |
| Agent Gateway/Registry | Capabilities, permissions, MCP allowlists, audit |
| MCP | In-process typed catalog; chưa phải remote MCP transport |
| RAG/vector DB | Qdrant adapter + deterministic sample embedding |
| API/UI | Authenticated v1 JSON/SSE + same-origin evidence-first client |
| Deployment | Alembic, Redis/Qdrant/PostgreSQL Compose, hardened app image, CI |
| Evaluation | Frozen 28-case offline corpus; no unearned baseline comparison |

Khi thay dữ liệu thật, thứ tự mở rộng an toàn là: data contract/quality gate →
versioned real embedding/index → shadow evaluation → frozen single-agent
baseline → calibrated analytics/ranking → load/chaos tests → distributed rate
limit/telemetry → cân nhắc tách service theo bottleneck đo được.
