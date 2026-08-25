# Runbook vận hành

## 1. Production prerequisites

- Docker Engine/Compose v2 hoặc runtime tương đương.
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
| `OPENAI_API_KEY` | Tùy chọn | Không dùng bởi v1 deterministic runtime |
| `LOG_LEVEL` | `INFO` khuyến nghị | Không bật debug chứa payload ở production |

`POSTGRES_PASSWORD` và `REDIS_PASSWORD` trong Compose phải URL-safe. Nếu dùng
managed service có ký tự đặc biệt, percent-encode đúng connection URL hoặc cung
cấp trực tiếp URL đã mã hóa ngoài Compose template.

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

## 4. Deploy và bootstrap

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
4. `docker compose up --build -d`; job migrate chạy trước backend.
5. Chờ `/readyz`, chạy smoke JSON + SSE, kiểm tra metrics/errors.
6. Theo dõi latency/error rate và one-shot job logs.

Không dùng `seed --reset` cho upgrade. Sample seed mặc định chỉ xác nhận snapshot
idempotent và sẽ dừng nếu gặp dữ liệu ngoài dự kiến.

## 9. Rollback

Application rollback:

- dùng image tag/commit trước;
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

## 11. Thay dữ liệu mẫu bằng dữ liệu thật

Không chạy sample seed trên database thật. Quy trình tối thiểu:

1. định nghĩa data contract, consent/licensing, PII retention và quality checks;
2. tạo migration/import job riêng, idempotency key và staging environment;
3. version corpus/index/model, provenance source IDs và rollback;
4. thay sample labels trên UI/API, không chỉ đổi connection string;
5. curator review gold evaluation và thu frozen baseline mới;
6. shadow run, load test, backup/restore drill trước cutover;
7. theo dõi drift, complaint false-positive và recommendation fairness.
