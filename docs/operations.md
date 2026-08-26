# Runbook vận hành

## 1. Production prerequisites

- Docker Engine/Compose v2 cho profile production-like; Helm 3 + Kubernetes cho
  chart production, và kind cho smoke cluster local.
- Reverse proxy TLS; backend bind loopback/private network.
- Secret manager hoặc file `.env` được giới hạn quyền đọc và không commit.
- Dung lượng bền cho PostgreSQL/Qdrant; quyết định rõ Redis session persistence.
- External monitoring gọi `/readyz` và scrape `/metrics` bằng operations key.
- Backup/restore test trước khi thay dữ liệu thật hoặc upgrade schema.

Container tags trong `compose.yaml` được pin theo version thay vì floating
`latest`. Khi nâng tag, review release notes và chạy lại full CI + restore drill.

## 2. Cấu hình

| Variable | Production | Mô tả |
| --- | --- | --- |
| `APP_ENV` | `production` | Compose đặt cố định |
| `DATABASE_URL` | PostgreSQL authenticated | Compose dựng từ `POSTGRES_PASSWORD` |
| `GATEWAY_API_KEYS` | Bắt buộc, không demo | `principal:secret[,principal:secret]` |
| `OPERATIONS_API_KEY` | Bắt buộc, ≥16 chars | Không dùng chung user API key |
| `SHARED_STATE_BACKEND` | `redis` | Production validator bắt buộc |
| `REDIS_URL` | Authenticated `redis[s]://` | Compose dựng từ `REDIS_PASSWORD` |
| `SESSION_TTL_SECONDS` | 60..2.592.000 | Mặc định 3.600 |
| `KNOWLEDGE_BACKEND` | `qdrant` | Production validator bắt buộc |
| `QDRANT_API_KEY` | Bắt buộc, ≥16 chars | Không chấp nhận placeholder |
| `ORCHESTRATION_TIMEOUT_SECONDS` | 1..300 | Mặc định 30 |
| `LEGACY_CHAT_ENABLED` | `false` | Production validator bắt buộc |
| `MODEL_RUNTIME_MODE` | `hybrid` hoặc `required` | `off`, `shadow`, `hybrid`, `required` |
| `OPENAI_API_KEY` | Bắt buộc nếu model runtime bật | Secret provider, không log/render vào values |
| `OPENAI_*_MODEL` | Nano/Mini theo stage | Routing/planning/specialist/synthesis độc lập |
| `OPENAI_REQUEST_TIMEOUT_SECONDS` | 1..120 | Mặc định 18 giây/call |
| `OPENAI_MAX_RETRIES` | 0..5 | Mặc định 2; còn có concurrency/circuit budget |
| `EMBEDDING_BACKEND` | `auto` hoặc `openai` | `hashing` chỉ khi chủ đích dùng vector baseline |
| `LOG_LEVEL` | `INFO` khuyến nghị | Không bật debug chứa payload ở production |

`POSTGRES_PASSWORD` và `REDIS_PASSWORD` trong Compose phải URL-safe. Nếu dùng
managed service có ký tự đặc biệt, percent-encode đúng connection URL hoặc cung
cấp trực tiếp URL đã mã hóa ngoài Compose template.

Production validator yêu cầu `OPENAI_API_KEY` khi `MODEL_RUNTIME_MODE` khác
`off`; `required` luôn cần key. Qdrant + `EMBEDDING_BACKEND=auto` ở production
cũng cần key. Muốn triển khai hoàn toàn không gọi provider phải đặt đồng thời
`MODEL_RUNTIME_MODE=off` và `EMBEDDING_BACKEND=hashing`; không để `hybrid` thiếu
key rồi dựa vào fallback ngầm.

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
pytest -m "not integration" -q
ruff check app tests migrations
mypy app
pytest -q tests/test_helm_chart.py tests/test_compose_config.py
```

`requirements.lock` là runtime lock được image sử dụng; `requirements-dev.lock`
include lock runtime và pin các công cụ kiểm thử. `frontend/package-lock.json`
khóa toolchain web; Vite sinh `app/frontend/dist` trước khi chạy Python test hoặc
đóng wheel. Không sửa hay commit output này. Khi đổi dependency, cập nhật lock
file tương ứng trong cùng commit và chạy lại build image/CI.

Kiểm tra image build ở host có Docker daemon:

```powershell
docker build --tag ecommerce-multi-agent:1.0.0 .
```

Không deploy nếu test/Compose/image build thất bại, nếu migration chưa review,
hoặc chưa có backup hợp lệ cho DB đang chứa dữ liệu thật.

## 4. Triển khai

### 4.1 Docker Compose production-like

```powershell
docker compose --env-file .env up --build -d
docker compose --env-file .env ps
```

One-shot services:

- `migrate`: Alembic upgrade đến head;
- `seed-data`: xác nhận/ghi đúng sample snapshot, từ chối DB khác;
- `seed-knowledge`: đợi Qdrant tối đa 90 giây, xác nhận collection rồi upsert
  market notes mẫu.

Backend chỉ khởi động sau khi các job cần thiết thành công. Xem log theo service,
không dump environment:

```powershell
docker compose --env-file .env logs --tail 100 migrate seed-data seed-knowledge
docker compose --env-file .env logs --tail 100 backend
```

### 4.2 Helm production

Profile mặc định chỉ triển khai application; PostgreSQL, Redis và Qdrant phải là
managed/external services. Chart không tạo Secret. Tạo Secret `existingSecret`
qua secret manager/External Secrets/GitOps sealed-secret workflow của môi trường,
không đặt secret trong `values.yaml`, `--set` hoặc manifest commit. Các key mặc
định cần có:

```text
DATABASE_URL
REDIS_URL
GATEWAY_API_KEYS
OPERATIONS_API_KEY
QDRANT_API_KEY
OPENAI_API_KEY
```

`GATEWAY_API_KEYS` và `OPERATIONS_API_KEY` là hai credential plane riêng. Mỗi
gateway secret và operations secret phải dài ít nhất 16 ký tự; database/Redis
URL phải authenticated. Nếu key name trong secret manager khác, map qua
`secretKeys.*` thay vì copy secret.

Render/lint trước, rồi install bằng image immutable đã push. Chỉ truyền non-secret
configuration trong deployment-specific values file:

```powershell
helm lint deploy/helm/ecommerce-multi-agent
helm template ecommerce-multi-agent deploy/helm/ecommerce-multi-agent `
  --namespace ecommerce `
  --set existingSecret=ecommerce-multi-agent-runtime `
  --set config.qdrantUrl=https://qdrant.internal.example

helm upgrade --install ecommerce-multi-agent `
  deploy/helm/ecommerce-multi-agent `
  --namespace ecommerce `
  --create-namespace `
  --set existingSecret=ecommerce-multi-agent-runtime `
  --set image.repository=REGISTRY/ecommerce-multi-agent `
  --set image.tag=IMMUTABLE_TAG `
  --set config.qdrantUrl=https://qdrant.internal.example `
  --wait `
  --timeout 10m
```

Alembic chạy bằng pre-install/pre-upgrade hook. Production schema chặn internal
dependencies, sample data/knowledge seed và replica khác `1`. Không tăng replica
cho tới khi rate limiter/telemetry được externalize. Service là `ClusterIP`;
Ingress mặc định tắt để TLS, hostname và controller policy do platform owner
quyết định.

Nếu cluster có Prometheus Operator và Grafana sidecar, bật monitoring resources:

```powershell
helm upgrade --install ecommerce-multi-agent `
  deploy/helm/ecommerce-multi-agent `
  --reuse-values `
  --set monitoring.serviceMonitor.enabled=true `
  --set monitoring.prometheusRule.enabled=true `
  --set monitoring.grafanaDashboard.enabled=true `
  --wait
```

ServiceMonitor đọc Bearer credential trực tiếp từ key `OPERATIONS_API_KEY` trong
existing Secret. Prometheus CRDs phải tồn tại trước khi bật; chart không cài
Prometheus Operator.

### 4.3 kind smoke profile

Profile kind dựng PostgreSQL/Redis/Qdrant nội bộ với PVC, migrate rồi seed sample
data/knowledge trước khi application Ready. Nó cố ý dùng model `off` + hashing
embedding để smoke hạ tầng không tiêu provider quota; đây không phải cấu hình
chất lượng production.

BuildKit có thể sinh attestation multi-manifest mà `kind load docker-image` không
chọn đúng platform, nên image smoke được build với `--provenance=false`:

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
  --from-literal=QDRANT_API_KEY='kind-qdrant-secret-2026' `
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

Trong terminal khác:

```powershell
python scripts/ci_live_smoke.py
```

Expected: application/PostgreSQL/Redis/Qdrant Ready, init containers
`wait-for-dependencies`, `migrate`, `seed-data`, `seed-knowledge` exit `0`, không
restart; JSON, SSE, auth, readiness và metrics smoke pass. Sau khi kiểm tra xong,
xóa cluster local bằng `kind delete cluster --name ecommerce-ma`. Các credential
trên chỉ là synthetic secret cho cluster disposable, tuyệt đối không tái sử dụng.

## 5. Health/readiness verification

```powershell
Invoke-RestMethod http://127.0.0.1:8000/livez
Invoke-RestMethod http://127.0.0.1:8000/readyz
```

Expected readiness production:

```json
{
  "status": "ready",
  "checks": {
    "runtime": "ok",
    "database": "ok",
    "redis": "ok",
    "qdrant": "ok"
  }
}
```

`database: ok` ở production đồng nghĩa kết nối được và `alembic_version` đúng
revision ứng dụng mong đợi. `qdrant: ok` đồng nghĩa health endpoint phản hồi,
collection đúng vector size/distance và có ít nhất một knowledge point; nó không
chỉ là TCP/service liveness.

Smoke chat bằng credential thử riêng, không dùng operations key:

```powershell
$headers = @{"X-API-Key" = "ROTATABLE_SMOKE_SECRET"}
$body = @{message = "Tìm Nova Air S2"} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/chat `
  -Headers $headers -ContentType "application/json" -Body $body
```

Xóa/rotate smoke credential sau xác nhận nếu policy yêu cầu.

## 6. Metrics và diagnosis

```powershell
$ops = @{"X-Operations-Key" = "OPERATIONS_SECRET"}
Invoke-WebRequest http://127.0.0.1:8000/metrics -Headers $ops
Invoke-RestMethod http://127.0.0.1:8000/api/v1/operations/agents -Headers $ops
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/operations/audit?limit=50" -Headers $ops
```

Prometheus-compatible scrape cũng có thể dùng Bearer auth:

```powershell
$bearer = @{Authorization = "Bearer OPERATIONS_SECRET"}
Invoke-WebRequest http://127.0.0.1:8000/metrics -Headers $bearer
```

Metrics gồm HTTP/agent/model counters, readiness dependency counters và latency
histograms với bounded labels/buckets. Không tạo dashboard query theo raw URL,
request ID, user/session ID hoặc model response ID vì sẽ gây cardinality cao.
Grafana dashboard và alert rules trong chart theo dõi availability, readiness,
5xx/429, latency và model fallback/circuit signals.

Khi client báo lỗi, lấy `request_id`/`trace_id` từ body hoặc headers rồi tra:

```powershell
Invoke-RestMethod `
  "http://127.0.0.1:8000/api/v1/operations/traces/trace_EXAMPLE" `
  -Headers $ops
```

Trace/audit là ring buffer trong process; restart sẽ mất. Log collector bên
ngoài phải capture access/application logs nếu cần retention dài.

## 7. Backup và restore

Trước migration/data replacement, tạo PostgreSQL backup theo công cụ/chính sách
của môi trường. Ví dụ logical backup (PowerShell ghi file local):

```powershell
docker compose --env-file .env exec -T postgres `
  pg_dump -U ecommerce -d ecommerce -Fc > ecommerce-backup.dump
```

Xác nhận file khác rỗng và thử restore vào **database kiểm thử riêng**. Không
restore đè production trong routine deploy. Qdrant hiện chỉ chứa market notes
mẫu nên có thể dựng lại bằng `seed-knowledge`; khi thay bằng corpus thật phải có
snapshot/index rebuild procedure riêng. Redis chứa state TTL, không phải nguồn
sự thật nghiệp vụ.

## 8. Upgrade

1. Backup và ghi lại image tag/commit hiện hành.
2. Review Alembic upgrade/downgrade; test trên bản sao DB.
3. Build image mới và chạy offline suite/evaluation.
4. Compose: `docker compose up --build -d`; Helm: `helm upgrade --install ...
   --wait`; migration job/hook phải thành công trước rollout.
5. Chờ `/readyz`, chạy smoke JSON + SSE, kiểm tra metrics/errors.
6. Theo dõi latency/error rate và one-shot job logs.

Không dùng `seed --reset` cho upgrade. Sample seed mặc định chỉ xác nhận snapshot
idempotent và sẽ dừng nếu gặp dữ liệu ngoài dự kiến.

## 9. Rollback

Application rollback:

- Compose dùng image tag/commit trước; Helm dùng `helm history` rồi chỉ
  `helm rollback` khi schema vẫn backward-compatible;
- chỉ rollback app khi schema mới còn backward-compatible;
- nếu không compatible, dùng kế hoạch migration rollback đã review và backup đã
  thử restore, không tự động gọi destructive downgrade.

Rollback không được làm bằng `git reset --hard`, xóa volume hay sửa trực tiếp
tables. Named volumes là dữ liệu bền; `docker compose down -v` sẽ xóa dữ liệu và
không thuộc quy trình thường lệ.

## 10. Incident hints

| Triệu chứng | Kiểm tra | Hành động an toàn đầu tiên |
| --- | --- | --- |
| `/livez` ok, `/readyz` 503 DB | Postgres health/log/disk và Alembic revision | Dừng route traffic; không reseed/reset |
| Redis failed | Auth/URL/network | Giữ traffic off; session API sẽ 503 fail closed |
| Qdrant failed | API key/vector contract/point count/storage | Giữ traffic off; rerun bounded seed nếu chỉ là sample index |
| 429 tăng | Auth abuse hoặc budget thấp | Kiểm tra peer/principal metrics; không tắt limiter |
| 409 session busy | Client gửi song song cùng session | Client backoff/serialize turn |
| 504 | Agent/data latency | Tra trace; kiểm tra downstream trước khi tăng timeout |
| 503 all agents failed | Agent error codes/audit | Giữ answer fail closed; không bật fallback bịa facts |
| Model fallback tăng | Provider timeout/rate limit/schema/policy/circuit | Tra model stage/error code; giữ deterministic path, không tăng retry vô hạn |
| `required` trả 503/504 | Provider hoặc evidence authorization fail | Giữ traffic có kiểm soát; không đổi sang `hybrid` nếu SLO bắt buộc model chưa được owner duyệt |

## 11. Thay dữ liệu mẫu bằng dữ liệu thật

Không chạy sample seed trên database thật. Quy trình tối thiểu:

1. định nghĩa data contract, consent/licensing, PII retention và quality checks;
2. tạo migration/import job riêng, idempotency key và staging environment;
3. version corpus/index/model, provenance source IDs và rollback;
4. thay sample labels trên UI/API, không chỉ đổi connection string;
5. curator review gold evaluation và thu frozen baseline mới;
6. shadow run, load test, backup/restore drill trước cutover;
7. theo dõi drift, complaint false-positive và recommendation fairness.
