# Đối chiếu 8 giả thiết performance — 03/10/2026

Ảnh review trình bày giả thiết và thứ tự điều tra, không phải kết luận về nguyên
nhân hay p95 production. Đối chiếu dưới đây dùng source hiện tại, local regression
và provider giả; không gửi thêm request tới OpenAI để đo latency.

| Mục | Kết luận từ code và phép kiểm | Trùng remediation / xử lý |
| --- | --- | --- |
| H1 — nhiều model stages nối tiếp | Đúng có điều kiện: planner, specialist selector, answer draft, semantic verifier và repair tối đa một lần; mode off/failed/no-evidence có thể bỏ stage. Chưa đo tỷ trọng latency thực. | Trùng E11 về runtime v2. Giữ các stage cần cho evidence/grounding; không suy ra mọi query đều có số calls giống nhau. |
| H2 — operation độc lập vẫn tuần tự | Đúng: executor duyệt operations và await từng tool/specialist. Ba tool giả 50 ms có max inflight 1, mất 172–188 ms; gather control khoảng 63 ms chỉ minh họa overhead tuần tự. | Trùng E11. Sau khi user giao chọn phương án, giữ bounded sequential cho bản chốt; ready-wave parallel cần kiểm fencing/replay/cancel/budget riêng. |
| H3 — retrieval/catalog xử lý Python | Đúng một phần: hybrid ranking dùng Python trên chunks đã lọc ACL; sandbox catalog lọc/xếp trong Python. Catalog PostgreSQL thông thường đã đẩy các bộ lọc vào SQL; SQLite có Unicode fallback. | Sửa ranking chạy ngoài event loop, commit `ac5f358`; 18 retrieval regressions đạt. Thuật toán ranking và bounds giữ nguyên, chưa chứng minh khả năng scale hoặc p95. |
| H4 — text chờ terminal | Progress đến sớm; text deltas phát sau durable completion rồi terminal. Đây là delta trên answer đã grounding, không phải stream raw provider tokens. | Trùng E11; khác lỗi cancel E7 đã sửa. Giữ publication boundary để không hiển thị claim chưa kiểm chứng. |
| H5 — sync SQL và budget 2 | Trước fix, lifecycle SQL đã offload nhưng bốn cancellation preflight vẫn gọi fresh SQL probe trực tiếp. Global cap 2 là giới hạn đồng thời, vượt cap bị từ chối, không có queue chờ slot. | Trùng E10; sửa phần còn sót trong `313e86a`. Probe giả 200 ms từng chặn heartbeat khoảng 188–203 ms trước fix; 25 focused tests đạt sau fix. Giữ cap 2 và fresh cancellation checks. |
| H6 — retry/timeout kéo tail | Đúng về policy: budget retry tối đa một lần, timeout mỗi attempt 18 s; SDK retry trong budget path bằng 0. Hai provider attempts cộng backoff có mức tối đa theo số học 36,15 s; đây không phải end-to-end bound, pipeline còn deadline 60 s. | Chưa có bằng chứng retry lặp lớp hoặc p95 production. Unscoped fake generation với retry 10 ms cộng khoảng 162 ms do backoff 150 ms và attempt thứ hai; phép thử này không đo budgeted v2 path. Không hạ timeout chỉ từ giả thiết. |
| H7 — reopen và SQL theo product | Trước fix, review/trust fetch products theo batch nhưng đọc lại product và review riêng cho từng sách. Reopen knowledge giữa các pass áp lại ACL hiện hành; một pass đã khử duplicate evidence IDs. | Sửa batch review trong `69e457b`: một query review cho mọi product, vẫn tối đa 20/sách, thứ tự NULLS LAST và ID giữ nguyên. Giữ reauthorization giữa các pass; không cache ACL qua provider wait. |
| H8 — cold service graph | Graph dựng lần đầu, cache bằng double-checked lock; route resolve chạy trên worker. Cold path có catalog/corpus/index validation và runtime construction; không rebuild mỗi request. | Trùng một phần E8: health/readiness không thay strict chat/SSE/pin smoke. Fake cold 0,539 ms và warm median 0,0061 ms không đo cold PostgreSQL/provider production; chưa có lỗi init cần rewrite. |

## Evidence và phạm vi

- `output/review-remediation/performance-h1245-verification.v1.json`: source
  call sites, ba operation giả và event ordering; không provider thật.
- `output/review-remediation/perf-h6-h8-offline.json`: unscoped fake generation retry và
  cold/warm construction counts; không database thật.
- `output/review-remediation/perf-h3-h7-review.json`: independent checks cho
  NULL ordering, missing/no-review/domain guards, ranking cancel và SQL compile.
- `ranking-worker-before.xml` tái hiện loop blocking; sau sửa,
  `ranking-worker-after-ranking.xml` ghi 18 tests đạt.
- `review-batching-before-behavior.xml` tái hiện query lặp; `review-batching-after.xml`
  ghi 40 SQLite tests đạt. `review-batching-final-null-check.xml` ghi thêm ba
  checks đạt với NULL timestamps thực sự nằm trong sample.
- `perf-postgres-regressions.xml` ghi 54 tests đạt trên PostgreSQL fixture riêng
  cho tools, SQL boundaries và actual catalog. Đây là regression có providers
  giả, không phải benchmark latency/quality; source scope trước followup bốn
  cancellation preflights của H5. Đã dừng fixture sau test.
- `source-checks.v4.json` và `evaluator-runtime-regressions.v4.xml`: source cuối
  `04e61e6`, 620 tests/41 affected modules đạt, 25 integration deselected;
  Ruff/format/mypy đạt và 289 source/test hashes không đổi trong lượt chạy.
  Không gọi provider thật; kết quả này không thay benchmark latency/quality.

Các số test ở các artifact có overlap, không cộng thành tổng unique tests.
Nguồn mới cần protocol/run freeze mới trước P7/P8; các kết quả historical không
được gắn thành kết quả performance của source sau sửa. Thử tải nhiều users,
provider latency thật và cold deployment trace vẫn chưa đo.
