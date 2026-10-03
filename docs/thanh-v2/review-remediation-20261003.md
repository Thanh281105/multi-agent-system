# Remediation review trước bảo vệ — 03/10/2026

Audit baseline: `42311032c3ed59147ae19e6f3e5727cee0eff93b`, nhánh `thanh-v3`.
Các sửa đổi được chia theo issue và phải có regression cùng evidence tương ứng.
Không sửa snapshot, gold, protocol hay report đã freeze tại chỗ.

## Scope và gates

| Issue | Required gate | Trạng thái |
| --- | --- | --- |
| E1 price/rating/pages | Field-aware typed parser + actual catalog regression | Đang triển khai |
| E2 fresh entities/review | Resolve từng title, ambiguity, fresh compare/review | Đang triển khai |
| E3 book domain | Book guard cho candidate, aggregate, direct IDs và sandbox | Đang triển khai |
| E4 semantic/numeric judge | Independent numeric/adversarial/per-claim evaluator tests | Đang triển khai |
| E5 citations/retrieval metrics | Binding/field matching; không claim document recall thiếu authority | Đang triển khai |
| E6 quality evidence | Absolute/category historical results; corrected successor | Historical tables đã đối chiếu; successor pending |
| E7 cancel before admission | Stalled first-event/idle timeout + persisted cancel identity | Đang triển khai |
| E8 deployed runtime | Helm pins + strict conversation/JSON/SSE/knowledge smoke | Đang triển khai |
| E9 receipt integrity | Bind/rederive claims và runtime metadata | Đang triển khai |
| E10 sync lifecycle SQL | Worker-scoped sessions, bounded DB waits, heartbeat/cancel regression | Pending |
| E11 docs | Tách public v2, historical v1 và single-agent fixture | Đã sửa, chờ documentation gate |

## Successor evidence

P7 v18/P8 v23 giữ nguyên làm historical evidence sau source remediation. Thứ tự
bắt buộc: source/input/evaluator freeze → P7 complete → repeat freeze → P8
720/720 → exact evidence → valid independent calibration → automated judgments
→ receipt-bound join → report/hash validation. Không dùng diagnostic aliases,
partial run hoặc green health check thay cho gate này.

Đối chiếu v23 có task successes 9/180 ở ba variants RAG và 12/180 ở adaptive
no-RAG. Xem [bảng theo category và caveats](../evaluation.md). Chưa có evidence
cho positive MA/RAG quality conclusion. Independent semantic reference labels
cần ghi rõ automated hay human và bind đúng packet/case; calibration theo
substring không là semantic truth.

## Demo rehearsal

- [ ] PostgreSQL head `20260910_0008`, catalog 200 records/1.773 reviews và hashes.
- [ ] Published corpus/index pins, compatible embeddings và source reopening.
- [ ] Owner-bound conversation + JSON chat thành công, không nhận 503 là pass.
- [ ] POST SSE có progress, grounded result/citation và đúng một terminal.
- [ ] Fresh tìm sách, so sánh hai title, natural review, recommendation sách.
- [ ] Known-work knowledge query có final exact citation và unsupported query.
- [ ] Cancel trước event đầu, sau admission, reconnect/retry giữ identity.
- [ ] Browser console/focus/keyboard/citation và mobile/zoom cơ bản.
- [ ] Offline backup có index/query embedder tương thích; generation off không
  tự tắt embeddings. Ghi rõ backup là live offline hay recorded.

Tải 10/100/500 users, multi-replica, TLS/public deployment, human adjudication
và manuscript/slides chưa được xác minh trong remediation này. Không suy ra các
claim đó từ unit tests hoặc single-user rehearsal. Không cần mở rộng kiến trúc
để hoàn thành scoped thesis/demo gates.
