# Vietnamese E-commerce Agent — Phase 1

Proof of concept cho một **single agent** bằng OpenAI Responses API với model
`gpt-5.4-mini`. Người dùng hỏi bằng tiếng Việt; agent chọn một trong ba Python
tools, tool đọc PostgreSQL qua SQLAlchemy, rồi agent tạo grounded answer từ dữ
liệu đã trả về.

Phase này cố ý chưa có multi-agent, orchestrator, A2A, MCP, RAG, vector
database, Redis, frontend, SSE hay authentication.

## Architecture

```text
Client
  ↓ HTTP
FastAPI (/chat)
  ↓ validated ChatRequest
OpenAI Responses API runner
  ↓
EcommerceAgent (instructions + function tools)
  ↓ chooses a tool
Plain Python tool
  ↓
EcommerceRepository
  ↓ SQLAlchemy session
PostgreSQL
  ↓ structured facts
OpenAI GPT-5.4 mini
  ↓ Vietnamese grounded answer
ChatResponse { answer, tool_calls, session_id }
```

Business logic không biết OpenAI SDK tồn tại:

- `app/repositories/ecommerce.py` chỉ biết SQLAlchemy models.
- `app/tools/ecommerce.py` là các Python function bình thường, mở session ngắn
  hạn và trả dictionary JSON-friendly.
- `app/agent/agent.py` chỉ đăng ký tools và định nghĩa instruction.
- `app/agent/runner.py` là boundary duy nhất gọi Responses API, dispatch
  function tools và thu thập tool-call metadata.

## Project tree

```text
.
├── app/
│   ├── main.py
│   ├── api/chat.py
│   ├── agent/{agent.py,runner.py}
│   ├── tools/ecommerce.py
│   ├── db/{base.py,session.py,seed.py}
│   ├── models/{shop.py,product.py,review.py}
│   ├── repositories/ecommerce.py
│   ├── schemas/chat.py
│   └── core/config.py
├── tests/
│   ├── conftest.py
│   ├── test_health.py
│   ├── test_search_products.py
│   ├── test_product_reviews.py
│   ├── test_compare_products.py
│   ├── test_chat_validation.py
│   └── test_agent_integration.py
├── Dockerfile
├── compose.yaml
├── pyproject.toml
├── .env.example
└── README.md
```

## Setup bằng Docker Compose

1. Copy `.env.example` thành `.env` và đặt `OPENAI_API_KEY` nếu muốn gọi
   GPT-5.4 mini thật. Repo đã có sẵn `.env` placeholder cho local setup.
2. Chạy:

   ```bash
   docker compose up --build
   ```

Compose khởi động PostgreSQL, đợi healthcheck, chạy seed idempotent rồi chạy
backend tại `http://localhost:8000`. Nếu không có API key, `/health`, seed và
tool tests vẫn chạy; `/chat` sẽ trả lỗi 503 thay vì giả lập câu trả lời.

Seed có thể chạy lại an toàn:

```bash
docker compose run --rm backend python -m app.db.seed
```

Output:

```text
Seed complete
Shops: 5
Products: 30
Reviews: 150
```

Seed dùng `random.seed(42)` và explicit IDs. Các product quan trọng gồm:

- ID 1 — `Tai nghe Bluetooth Nova Air S2`
- ID 2 — `Tai nghe Gaming Sonic G5`
- ID 3 — `Tai nghe không dây Echo Buds Pro`

## Chạy local không dùng Docker

Cần Python 3.12+ và PostgreSQL đang chạy.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m app.db.seed
uvicorn app.main:app --reload
```

`create_all` được dùng thay cho Alembic vì Phase 1 chỉ có schema nhỏ và chưa
có migration lifecycle. Khi schema bắt đầu có dữ liệu production hoặc cần
rollback/versioning, chuyển sang Alembic là bước phù hợp.

## API

Health check:

```bash
curl http://localhost:8000/health
```

```json
{"status":"ok"}
```

Chat request:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Tìm cho tôi tai nghe dưới 1 triệu, rating ít nhất 4.5"}'
```

Response shape:

```json
{
  "answer": "...",
  "tool_calls": [
    {
      "name": "search_products",
      "arguments": {
        "category": "Tai nghe",
        "max_price": 1000000,
        "min_rating": 4.5
      },
      "result_summary": {"count": 2, "products_count": 2}
    }
  ],
  "session_id": "sess_..."
}
```

`tool_calls` chỉ có tên tool, arguments và summary. Không có chain-of-thought.
Nếu client truyền `session_id`, runner giữ response ID trong memory để dùng
lại context cho follow-up; nếu không, API sinh ID mới.

## Tests

Unit tests không gọi LLM thật và không cần PostgreSQL: `tests/conftest.py`
đổi session factory sang SQLite in-memory, nhưng vẫn chạy cùng repository và
tool functions.

```bash
pytest -m "not integration"
ruff check app tests
mypy app
```

The default suite also contains deterministic fake-Responses smoke tests. They
exercise the tool loop and dispatch without an external API call; only
`test_agent_integration.py` requires an OpenAI key.

Integration test có thể chạy có chủ đích khi đã đặt API key:

```bash
pytest -m integration
```

## Four demo queries

1. **Search**

   `Tìm cho tôi tai nghe dưới 1 triệu, rating ít nhất 4.5`

   Agent nên gọi `search_products`; dữ liệu seed có Nova Air S2 và Sonic G5.

2. **Reviews**

   `Khách hàng đánh giá thế nào về Tai nghe Bluetooth Nova Air S2?`

   Agent tìm ID 1 rồi gọi `get_product_reviews`, sau đó tóm tắt điểm mạnh
   (âm thanh/giao hàng/pin) và nhược điểm (đeo lâu, pin khi mở lớn).

3. **Compare**

   `So sánh Nova Air S2 và Sonic G5. Nếu ưu tiên giá và rating thì nên chọn sản phẩm nào?`

   Agent resolve ID 1 và 2 rồi gọi `compare_products`; tool chỉ trả facts, agent
   mới suy luận recommendation.

4. **Hallucination guard**

   `Đánh giá giúp tôi Tai nghe SuperDragon X999.`

   Search trả `count: 0`; agent phải nói không tìm thấy dữ liệu, không tự tạo
   giá, rating, review hay thông số.

## Request flow của Demo 1

```text
POST /chat
  → app/schemas/chat.py validates ChatRequest
  → app/api/chat.py creates request_id/session_id
  → app/agent/runner.py gets the session's previous OpenAI response ID
  → app/agent/agent.py supplies EcommerceAgent + instructions + tool schemas
  → GPT-5.4 mini returns a function call for search_products
  → runner dispatches the Python function (the LLM does not execute Python)
  → app/tools/ecommerce.py validates arguments and opens one DB session
  → app/repositories/ecommerce.py builds a SQLAlchemy SELECT
  → PostgreSQL filters products and returns rows
  → repository serializes rows into dictionaries
  → runner sends function_call_output back to OpenAI
  → GPT-5.4 mini writes a Vietnamese answer grounded in those facts
  → runner collects final response text and tool-call metadata
  → ChatResponse is returned as JSON
```

### OpenAI function calling, without framework magic

The Python function is registered as an OpenAI function tool with an explicit
JSON schema. The actual protocol is:

```text
Python function signature
  → OpenAI function/tool schema
  → model receives user message, instructions, and schema
  → model emits tool name + JSON arguments
  → runner validates and dispatches the Python function
  → function returns a structured dictionary
  → runner emits function_call_output and sends it to the model
  → model emits the final answer
```

`app/agent/runner.py` reads function-call output items, executes only the
registered tools, builds a small result summary, and selects `output_text` as
the final answer. It never exposes hidden model reasoning.

## Layer responsibilities

| Layer | Responsibility | Called by / calls next | Isolated test |
| --- | --- | --- | --- |
| FastAPI route | HTTP status, request IDs, error boundary | Client → runner | `test_health.py`, validation tests |
| Pydantic schema | Validate/serialize API contract | FastAPI | `test_chat_validation.py` |
| OpenAI agent | Instructions, model, tool registration | Runner → model | optional integration test |
| OpenAI runner | Session, function-call loop, tool-call visibility | API → agent | optional integration test |
| Tool | Validate arguments, open/close DB session, JSON facts | Runner → repository | `test_*` tool tests |
| Repository | SQLAlchemy queries, ORM-to-dict mapping | Tool → models | exercised with SQLite fixture |
| SQLAlchemy model | Tables, keys, constraints, relationships | Repository → DB | seed + schema smoke |
| DB session | Short-lived connection/session lifecycle | Tool → PostgreSQL | SQLite session fixture |
| PostgreSQL | Durable structured source of truth | SQLAlchemy | Docker seed/smoke |

Các layer không gộp vì mỗi boundary có failure/test contract riêng: HTTP
validation không cần DB, tools không cần LLM, repository không cần prompt, và
runner không nên chứa SQL.

## Current limitations

- Single agent only; không có Product/Review/Trust agents hay orchestrator.
- Synthetic deterministic dataset, không phải dữ liệu marketplace thật.
- Session chỉ in-memory và mất khi process restart.
- Không có RAG, MCP, vector database, Redis, SSE, frontend hoặc production auth.
- OpenAI API key/network là dependency cho `/chat` thật; lỗi runtime trả 503,
  không fallback sang dữ liệu bịa.
- Session response IDs chỉ nằm trong process memory, chưa phải session service
  cho production multi-instance.
- `create_all` chưa phải migration framework.

## Future architecture (chưa implement)

```text
Single Ecommerce Agent
        ↓
Product / Review / Trust Agents
        ↓
Orchestrator
        ↓
Shared Platform
        ↓
MCP
        ↓
Agent Gateway / Registry
```

Phase 1 dừng ở đây để review architecture và code trước khi mở rộng Phase 2.
