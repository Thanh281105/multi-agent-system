# Runbook vận hành

## 1. Production prerequisites

- Docker Engine/Compose v2 cho production-like local; Helm 3 + Kubernetes cho
  cluster, kind chỉ dùng smoke disposable.
- Reverse proxy TLS; backend chỉ bind loopback/private network.
- Secret manager hoặc `.env` có quyền đọc giới hạn và không commit.
- PostgreSQL bền; Redis authenticated theo shared-state production settings.
  API v2 dùng PostgreSQL làm authority cho conversation, turn, action, ledger
  và published corpus/index.
- Backup/restore drill trước migration hoặc thay snapshot.
- External monitoring gọi `/readyz` và scrape `/metrics` bằng operations key.

Runtime mặc định dùng snapshot lịch sử Tiki Books eval (200 sách/1.773 review),
không phải feed Tiki trực tiếp. Qdrant là adapter lịch sử tùy chọn; API v2 dùng
knowledge mặc định trong `PostgresKnowledgeStore` và cần resolve published
PostgreSQL corpus/index đã pin. Repository không seed Qdrant knowledge corpus.

## 2. Cấu hình

| Variable | Production/default | Ghi chú |
| --- | --- | --- |
| `APP_ENV` | `production` | Compose đặt cố định |
| `DATABASE_URL` | Authenticated PostgreSQL | SQLite chỉ development/test |
| `PUBLIC_SNAPSHOT_DIR` | `data/snapshots/tiki-books-v4-eval` | Trong image là `/app/data/...` |
| `V2_CORPUS_VERSION_ID` | Published v2 corpus ID | Bắt buộc khi resolve API v2 |
| `V2_INDEX_MANIFEST_ID` | Published v2 index ID | Optional chỉ khi corpus có một complete index; production nên pin rõ |
| `GATEWAY_API_KEYS` | Bắt buộc, không demo | `principal:secret[,principal:secret]` |
| `GATEWAY_PRINCIPAL_POLICIES` | Bắt buộc | `principal:tenant:scope1|scope2` |
| `OPERATIONS_API_KEY` | Bắt buộc, ≥16 chars | Tách khỏi user key |
| `SHARED_STATE_BACKEND` | `redis` | Production validator bắt buộc |
| `REDIS_URL` | Authenticated `redis[s]://` | Shared-state adapter |
| `KNOWLEDGE_BACKEND` | `disabled` | Historical adapter; API v2 uses PostgreSQL knowledge |
| `MODEL_RUNTIME_MODE` | `hybrid` mặc định | `off`, `shadow`, `hybrid`, `required` |
| `OPENAI_API_KEY` | Khi model mode khác `off` | Không log/render vào values |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Phải khớp published v2 index |
| `OPENAI_EMBEDDING_DIMENSIONS` | `1536` | Phải khớp published v2 index |
| `ORCHESTRATION_TIMEOUT_SECONDS` | `30` | Hợp lệ 1..300 |
| `LEGACY_CHAT_ENABLED` | `false` | Production validator bắt buộc |
| `LOG_LEVEL` | `INFO` khuyến nghị | Không log request/provider payload |

Compose truyền tiếp `V2_CORPUS_VERSION_ID` và `V2_INDEX_MANIFEST_ID` từ shell
hoặc `.env` vào app container. Không cần mở hay chép giá trị secret để kiểm tra
route; corpus/index ID là binding dữ liệu server-side.

`POSTGRES_PASSWORD` và `REDIS_PASSWORD` trong Compose phải URL-safe vì được nội
suy vào connection URL. Muốn chạy không provider: đặt
`MODEL_RUNTIME_MODE=off` và giữ `KNOWLEDGE_BACKEND=disabled`; embedding settings
không được dùng cho đường v1 này. V2 vẫn phải khớp embedding model/dimension của
published index trước khi resolve knowledge.

Chỉ khi owner chủ động tích hợp Qdrant mới đặt `KNOWLEDGE_BACKEND=qdrant`,
`QDRANT_URL` và `QDRANT_API_KEY`. Collection phải được provision riêng, tương
thích vector contract và không rỗng trước readiness. Repository không có job
`seed-knowledge` và agent path hiện tại không dùng collection đó làm evidence.

## 3. Pre-deploy

```powershell
Copy-Item .env.example .env
# Thay placeholder bằng secret thật; không in nội dung .env ra terminal/log.
docker compose --env-file .env config --quiet

Push-Location frontend
npm ci
npm run lint
npm test
npm run build
Pop-Location

python -m pip install -r requirements-dev.lock
ruff check app tests migrations
ruff format --check app tests migrations
mypy app
pytest -m "not integration" -q
pytest -q tests/test_helm_assets.py tests/test_deployment_assets.py
```

Không deploy nếu migration, snapshot validation, tests, Compose render hoặc
image build thất bại. Image check:

```powershell
docker build --tag ecommerce-multi-agent:1.0.0 .
```

Trước khi mở API v2, xác nhận migration head `20260910_0008`, catalog snapshot
đã import và published corpus/index là `books-v1-calibrated-20260909` (20
sources/chunks/vectors, 200 mappings). V2 phải fail closed nếu thiếu
`V2_CORPUS_VERSION_ID`, index chưa complete hoặc embedding model/dimension không
khớp. Không dùng calibration/Q&A artifacts làm corpus data.

`app/frontend/dist` là output của Vite; không sửa tay. Image đóng gói đúng
`data/snapshots/tiki-books-v4-eval` và chạy non-root/read-only.

## 4. Docker Compose production-like

Tạo `.env` từ template. Các giá trị tối thiểu phải thay là
`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `GATEWAY_API_KEYS`,
`GATEWAY_PRINCIPAL_POLICIES`, `OPERATIONS_API_KEY`; thêm `OPENAI_API_KEY` nếu
model mode bật. Không cần Qdrant key khi knowledge disabled.

```powershell
docker compose --env-file .env up --build -d
docker compose --env-file .env ps
```

Startup thực tế:

```text
postgres healthy → migrate (`ecommerce-migrate`)
                 → seed-data (`ecommerce-seed`)
                 → backend
redis healthy ────────────────────────────────┘
```

`seed-data` validate/import evaluation snapshot và từ chối database mixed,
legacy, khác profile hoặc khác hashes. Nó không sinh dữ liệu và không có reset
flag. Qdrant service chỉ khởi động nếu gọi Compose với profile `knowledge`, và
vẫn không có knowledge seed.

Xem log có giới hạn, không dump environment:

```powershell
docker compose --env-file .env logs --tail 100 migrate seed-data
docker compose --env-file .env logs --tail 100 backend
```

Backend chỉ publish ở `127.0.0.1:8000` theo default. Không expose trực tiếp ra
Internet; đặt sau reverse proxy TLS.

## 5. Helm

### 5.1 Production profile

Profile mặc định triển khai một application replica. PostgreSQL/Redis do
platform cung cấp; Qdrant không bắt buộc. Chart không tạo Secret. Existing
Secret tối thiểu chứa:

```text
DATABASE_URL
REDIS_URL
GATEWAY_API_KEYS
OPERATIONS_API_KEY
```

Kubernetes `secretKeyRef` cho `OPENAI_API_KEY` có `optional: true` chỉ để profile
model `off` có thể bỏ key. Với production mặc định `hybrid`, Existing Secret
phải có `OPENAI_API_KEY` không rỗng; runtime sẽ fail nếu thiếu. `QDRANT_API_KEY`
chỉ được tham chiếu khi `config.knowledgeBackend=qdrant`.

Production chart chạy Alembic hook nhưng `bootstrap.seedData=false`; việc import
snapshot và bind published corpus/index vào external database phải là data change
đã review riêng. Có thể chạy
cùng migration idempotent từ image/release tương ứng trong bounded operator job
trước khi mở traffic; Helm hook sẽ xác nhận lại revision khi rollout:

```powershell
ecommerce-data validate --snapshot data/snapshots/tiki-books-v4-eval
ecommerce-migrate
ecommerce-data import --snapshot data/snapshots/tiki-books-v4-eval
```

Sau đó đặt `V2_CORPUS_VERSION_ID` vào published corpus ID và pin
`V2_INDEX_MANIFEST_ID` vào published index ID. Không trỏ v2 vào Qdrant legacy seam
hoặc một index đang build.

Render/lint và deploy bằng immutable image:

```powershell
helm lint deploy/helm/ecommerce-multi-agent
helm template ecommerce-multi-agent deploy/helm/ecommerce-multi-agent `
  --namespace ecommerce `
  --set existingSecret=ecommerce-multi-agent-runtime

helm upgrade --install ecommerce-multi-agent `
  deploy/helm/ecommerce-multi-agent `
  --namespace ecommerce `
  --create-namespace `
  --set existingSecret=ecommerce-multi-agent-runtime `
  --set image.repository=REGISTRY/ecommerce-multi-agent `
  --set image.tag=IMMUTABLE_TAG `
  --wait `
  --timeout 10m
```

Chart dùng `ClusterIP`, NetworkPolicy, non-root/read-only security context và
chặn replica khác `1`. Không tăng replica trước khi externalize limiter và
telemetry. Monitoring resources là opt-in:

```powershell
helm upgrade --install ecommerce-multi-agent `
  deploy/helm/ecommerce-multi-agent `
  --reuse-values `
  --set monitoring.serviceMonitor.enabled=true `
  --set monitoring.prometheusRule.enabled=true `
  --set monitoring.grafanaDashboard.enabled=true `
  --wait
```

### 5.2 kind smoke profile

Kind profile chạy internal PostgreSQL/Redis, migration, Tiki snapshot bootstrap,
model `off` và knowledge `disabled`. Nó không chạy Qdrant hay `seed-knowledge`.

```powershell
docker build --provenance=false --tag ecommerce-multi-agent:kind .
kind create cluster --name ecommerce-ma `
  --image kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f
kind load docker-image ecommerce-multi-agent:kind --name ecommerce-ma

kubectl create namespace ecommerce-kind
kubectl -n ecommerce-kind create secret generic ecommerce-multi-agent-runtime `
  --from-literal=DATABASE_URL='postgresql+psycopg://ecommerce:kind-postgres-only@ecommerce-multi-agent-postgres:5432/ecommerce' `
  --from-literal=REDIS_URL='redis://:kind-redis-only@ecommerce-multi-agent-redis:6379/0' `
  --from-literal=GATEWAY_API_KEYS='kind:kind-gateway-secret-2026' `
  --from-literal=OPERATIONS_API_KEY='kind-operations-secret-2026' `
  --from-literal=POSTGRES_PASSWORD='kind-postgres-only' `
  --from-literal=REDIS_PASSWORD='kind-redis-only'

helm upgrade --install ecommerce-multi-agent `
  deploy/helm/ecommerce-multi-agent `
  --namespace ecommerce-kind `
  --values deploy/helm/ecommerce-multi-agent/values-kind.yaml `
  --wait `
  --timeout 10m

kubectl -n ecommerce-kind get pods
kubectl -n ecommerce-kind port-forward service/ecommerce-multi-agent 8000:8000
```

Trong terminal khác, chạy `python scripts/ci_live_smoke.py`. Expected:
PostgreSQL/Redis Ready; `wait-for-dependencies`, `migrate`, `seed-data` exit 0;
readiness/liveness, V2 identity/history (history có thể fail closed nếu chưa
publish corpus/index), V1 route trả 404, operations auth và metrics smoke pass.
Xóa cluster disposable bằng `kind delete cluster --name ecommerce-ma`.

## 6. Health và smoke

```powershell
Invoke-RestMethod http://127.0.0.1:8000/livez
Invoke-RestMethod http://127.0.0.1:8000/readyz
```

Default production-like readiness:

```json
{
  "status": "ready",
  "checks": {
    "runtime": "ok",
    "database": "ok",
    "redis": "ok",
    "knowledge": "disabled"
  }
}
```

Ở production, `database: ok` gồm đúng Alembic revision hiện hành. Redis chỉ xuất
hiện khi client được cấu hình. Nếu opt-in Qdrant thành công, knowledge trả
`"ok"`; false, collection rỗng hoặc contract sai làm readiness 503.

Smoke V2 bằng user credential riêng (lệnh này tạo một conversation lưu trong
PostgreSQL; nếu chưa publish corpus/index, V2 trả lỗi `v2.runtime_unavailable`):

```powershell
$headers = @{"X-API-Key" = "ROTATABLE_SMOKE_SECRET"}
$created = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v2/conversations `
  -Headers $headers -ContentType "application/json" `
  -Body '{"mode":"shopper"}'
$body = @{
  conversation_id = $created.conversation.conversation_id
  client_turn_id = "browser:smoke-$([guid]::NewGuid().ToString('N'))"
  message = "Tìm sách Quân Vương và cho biết tác giả"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v2/chat `
  -Headers $headers -ContentType "application/json" -Body $body
```

Bind `V2_CORPUS_VERSION_ID` và tùy chọn `V2_INDEX_MANIFEST_ID` trong môi trường
Compose để app resolve đúng snapshot PostgreSQL đã publish. Response phải giữ
provenance cho các claim dựa trên evidence.

## 7. Metrics và diagnosis

```powershell
$ops = @{"X-Operations-Key" = "OPERATIONS_SECRET"}
Invoke-WebRequest http://127.0.0.1:8000/metrics -Headers $ops
Invoke-RestMethod http://127.0.0.1:8000/api/v2/operations/agents -Headers $ops
Invoke-RestMethod "http://127.0.0.1:8000/api/v2/operations/audit?limit=50" -Headers $ops
```

Tra trace bằng `trace_id` từ response/header:

```powershell
Invoke-RestMethod `
  "http://127.0.0.1:8000/api/v2/operations/traces/trace_EXAMPLE" `
  -Headers $ops
```

Metrics/traces dùng labels bounded và không chứa raw user/session/model payload.
Trace/audit nằm trong ring buffer process và mất khi restart; cần collector ngoài
nếu muốn retention.

## 8. Backup, upgrade và rollback

Trước migration/data replacement, backup PostgreSQL theo policy môi trường và
restore thử vào database riêng. Ví dụ Compose logical backup:

```powershell
docker compose --env-file .env exec -T postgres `
  pg_dump -U ecommerce -d ecommerce -Fc > ecommerce-backup.dump
```

Upgrade:

1. Ghi image tag/commit và tạo backup đã kiểm tra.
2. Review migration và snapshot manifest/quality report.
3. Chạy offline suite, evaluation phù hợp và image/chart checks.
4. Chạy migration/import theo thứ tự, rồi rollout immutable image.
5. Chờ `/readyz`, smoke JSON/SSE và theo dõi errors/latency.

`ecommerce-seed` chỉ xác nhận/import đúng snapshot; nó không reset. Rollback app
chỉ hợp lệ khi schema backward-compatible. Không dùng `git reset --hard`, xóa
volume hoặc sửa table trực tiếp làm quy trình rollback.

Qdrant hiện không chứa corpus do repository sở hữu nên không có backup/rebuild
procedure cho legacy v1 adapter. V2 corpus/index trong PostgreSQL phải nằm trong
backup/restore drill và được kiểm tra identity trước khi mở traffic. Nếu thêm
Qdrant về sau, phải bổ sung versioned index backup và restore drill riêng.

## 9. Incident hints

| Triệu chứng | Kiểm tra | Hành động an toàn đầu tiên |
| --- | --- | --- |
| `/livez` ok, `/readyz` DB failed | PostgreSQL, disk, auth, Alembic revision | Dừng route traffic; không reseed/reset |
| Redis failed | URL/auth/network | Giữ traffic off; session API fail closed |
| `knowledge: failed` khi disabled mong đợi | Runtime config | Xác nhận `KNOWLEDGE_BACKEND=disabled`; không bật Qdrant để che lỗi |
| Opt-in Qdrant failed | URL/key/vector contract/point count | Giữ traffic off; provision corpus bằng quy trình riêng |
| Snapshot bootstrap refused | Manifest/hash/profile hoặc DB mixed/legacy | Không force; audit `dataset_sources` và dùng DB disposable/staging |
| 409 turn conflict | Client gửi lặp hoặc cạnh tranh cùng turn | Đọc lại trạng thái turn và retry theo contract |
| 429 tăng | Auth abuse hoặc budget thấp | Kiểm tra peer/principal metrics; không tắt limiter |
| 504 | Agent/data/provider latency | Tra trace/downstream trước khi tăng timeout |
| 503 all agents failed | Agent errors/audit | Giữ fail closed; không bịa fallback facts |
| Model fallback tăng | Timeout/rate/schema/policy/circuit | Kiểm tra stage/error code; không tăng retry vô hạn |

## 10. Dọn seed legacy disposable

Đây không phải bước startup/upgrade thường lệ. Chỉ với database disposable đã
xác nhận đúng fingerprint seed tổng hợp cũ:

```powershell
ecommerce-migrate
ecommerce-reset-legacy-seed `
  --confirm-disposable DELETE-LEGACY-SEED
ecommerce-seed --snapshot data/snapshots/tiki-books-v4-eval
```

Command bị chặn ở production và từ chối database không khớp exact legacy
fingerprint. Không dùng nó trên dữ liệu cần giữ.

## 11. V2 và Package 8 evaluation handoff

Handoff hiện hành sau sửa P02 nằm tại
[P02 successor handoff](thanh-v2/p02-successor-handoff.md), với protocol
`28621f0e6b7c1e8377b5b97bd9ebd7287054b58367979d00f558473d788f040c`.
Các hash `315eff…`/`c8a4…`/`f37e…` bên dưới là lịch sử trước sửa shopper
fixture. P7 successor v5 đã freeze; P8 successor v6-v9 có terminal partial
evidence nhưng chưa có kết quả benchmark hoàn chỉnh.
P8 exact evidence hiện đã có authority bất biến cho merchant inventory,
shopper cart/checkout preview reads, `catalog_product_N` và
`review_sample_N` khi receipt còn gắn đúng reset fixture/source asset,
namespace và authorization. Ranked catalog records, review samples vượt giới
hạn runtime, và mọi source không tái dựng đủ vẫn bị chặn fail-closed.

V2 smoke phải chạy trên PostgreSQL đã migrate tới `20260910_0008` và đã publish
corpus/index. Xác nhận các identity hiện hành trước khi gửi request:

```powershell
$env:V2_CORPUS_VERSION_ID = "cor_e06f6abbf338cfcf5fe17d450eec52ed961975e0fa8ee6bae32369ed3956"
$env:V2_INDEX_MANIFEST_ID = "idx_69af0802b50991c371bcb1f2954e79de82ccdc7855e15d3e602126b3b9c4"
Invoke-RestMethod http://127.0.0.1:8000/api/v2/me -Headers $headers
```

Tạo conversation rồi gửi `POST /api/v2/chat` với `conversation_id` và
`client_turn_id`; đọc lại `/api/v2/turns/{turn_id}` sau mọi disconnect. V2
response phải giữ durable status, pinned data versions, evidence bindings và
usage metadata. Một fixture hoặc SQLite success không thay thế được PostgreSQL
transaction/replay verification.

Historical P8 partial được giữ nguyên. Vì shopper fixture remediation đã đổi
source protocol, P7 successor v5 được chạy và freeze repeat decision trước khi
tạo P8 successors; không được resume historical P8 để redispatch terminal cells.
`run_p7_successor_v5` có schedule
`82617ce0e4004fcda0b542a1769de9653be044643f5189a5b3e88fa0aebd2163` cho 36
cells (4 warmup, 32 measurements), chọn 3 repeats. Package 8 vẫn là additive
held-out work. Các lệnh local không gọi provider:

```powershell
python -m app.evaluation.benchmark_cli validate
python -m app.evaluation.benchmark_cli dry-run --run-id run_p8_dry
python -m app.evaluation.benchmark_cli prepare `
  --output output/evaluation-v3/heldout
python -m app.evaluation.benchmark_cli partial-report `
  --output output/evaluation-v3/heldout-corrected-v3 `
  --run-id run_p8_heldout_corrected_v3
```

Live execution phải truyền explicit consent và durable database URL:

```powershell
python -m app.evaluation.benchmark_cli run `
  --allow-network `
  --database-url $env:DATABASE_URL `
  --output output/evaluation-v3/heldout

python -m app.evaluation.benchmark_cli resume `
  --allow-network `
  --database-url $env:DATABASE_URL `
  --output output/evaluation-v3/heldout

python -m app.evaluation.benchmark_cli operate `
  --allow-network `
  --database-url $env:DATABASE_URL `
  --judge-budget-nano-usd 250000000 `
  --output output/evaluation-v3/heldout
```

`operate` chỉ chạy sau checkpoint SUT đã complete; nó rebuild/verify preparation,
calibrate judge trên các pilot measurement đã freeze, freeze calibration, judge blind packet
và publish final report. Nó require explicit per-job budget; shared ledger vẫn
account tất cả attempt, retry và reservation. Citation catalog/review chỉ được
reopen từ source asset đã hash-pin và receipt authority tương ứng; record không
đủ provenance sẽ block lifecycle thay vì tự dựng evidence.

P8 hiện có bốn SUT successor run terminal partial, đều `0` pending, `0`
ambiguous và `0` orphan:

| Run | Schedule | Completed | Failed/missing |
| --- | --- | ---: | ---: |
| `run_p8_successor_v6` | `c0c41ae8…` | 718 | 2 |
| `run_p8_successor_v7` | `ad5b8d57…` | 716 | 4 |
| `run_p8_successor_v8` | `7aae9668…` | 714 | 6 |
| `run_p8_successor_v9` | `9efb06ceb5843f069f1e84e6f099ff57c5103b6a0167114db52e09d0bc24b394` | 712 | 8 |

V9 failure codes gồm 1 `attempt_timeout_limit_exceeded`, 2
`expert_selection_not_authorized`, 2 `model_response_incomplete` và 3
`model_response_invalid`. Không có raw provider payload hay fabricated citation
trong partial report. Cumulative live ledger là `10.387962730 USD` known và
`0.016542090 USD` unknown, không phải riêng v9. Dùng `partial-report` để
persist hoặc tái xác thực coverage/ledger/checkpoint hash; lệnh local-only và
không mở live runtime. Không báo cáo partial checkpoint như complete, scored,
calibrated hoặc judge-complete; `operate` chỉ chạy sau khi một SUT run đủ tất
cả receipt cells và các gate evidence/calibration/judging pass.
