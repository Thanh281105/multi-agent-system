# Remediation review trước bảo vệ — 03/10/2026

Audit baseline: `42311032c3ed59147ae19e6f3e5727cee0eff93b`, nhánh `thanh-v3`.
Các sửa đổi được chia theo issue và phải có regression cùng evidence tương ứng.
Không sửa snapshot, gold, protocol hay report đã freeze tại chỗ.

Source gate: Ruff/format và mypy đạt; full offline suite trên PostgreSQL fixture
riêng đạt **1.084 tests**, 27 integration tests deselected (858,24 giây).
JUnit: `output/review-remediation/offline-tests.xml`. Frontend clean install
từ lockfile, lint, 101 tests và production build đạt. Helm production/kind
rendering cùng negative pin validation đạt; strict smoke có thêm 8 tests đạt.

## Scope và gates

| Issue | Required gate | Trạng thái |
| --- | --- | --- |
| E1 price/rating/pages | Field-aware typed parser + actual catalog regression | Đã sửa; actual catalog/PG regressions đạt |
| E2 fresh entities/review | Resolve từng title, ambiguity, fresh compare/review | Đã sửa; fresh JSON/browser demo đạt |
| E3 book domain | Book guard cho candidate, aggregate, direct IDs và sandbox | Đã sửa; giữ 200 raw records, 199 sách đủ điều kiện v2 |
| E4 semantic/numeric judge | Independent numeric/adversarial/per-claim evaluator tests | Đã sửa; numeric, paraphrase, negation và claim-citation regressions đạt |
| E5 citations/retrieval metrics | Binding/field matching; không claim document recall thiếu authority | Đã sửa; document recall chưa đo khi thiếu relevance authority |
| E6 quality evidence | Absolute/category historical results; corrected successor | Historical tables đã đối chiếu; successor pending |
| E7 cancel before admission | Stalled first-event/idle timeout + persisted cancel identity | Đã sửa; timeout/controller tests và browser cancel/reconcile đạt |
| E8 deployed runtime | Helm pins + strict conversation/JSON/SSE/knowledge smoke | Helm rendering/pin validation và strict local PostgreSQL demo đạt |
| E9 receipt integrity | Bind/rederive claims và runtime metadata | Đã sửa; additive successor contract và tamper regressions đạt |
| E10 sync lifecycle SQL | Worker-scoped sessions, bounded DB waits, heartbeat/cancel regression | Đã sửa; worker ownership, slow DB, cancellation/settlement và PG recovery đạt |
| E11 docs | Tách public v2, historical v1 và single-agent fixture | Đã sửa; documentation integrity đạt |

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

- [x] PostgreSQL head `20260910_0008`, catalog 200 records/1.773 reviews và hashes.
- [x] Published corpus/index pins, compatible embeddings và exact live source reopening.
- [x] Owner-bound conversation + JSON chat thành công, không nhận 503 là pass.
- [x] POST SSE có progress, grounded result/citation và đúng một terminal.
- [x] Fresh tìm sách, so sánh hai title, natural review, recommendation sách.
- [x] Known-work knowledge query có final exact citation và unsupported query.
- [x] Cancel trước event đầu, sau admission, reconcile/reload giữ identity.
- [x] Browser console/focus/keyboard/citation và mobile/CSS zoom-equivalent cơ bản.
- [x] Backup **RECORDED** mở offline, không HTTP/external assets. Generation off
  không tự tắt embeddings; chưa có live offline embedder tương thích được rehearsal.

Strict gate cuối lưu tại `output/review-remediation/demo-rehearsal.json`:
6 public results, `strict_gate_passed=true`. `live-source-reopening.json`
xác nhận citation knowledge được mở lại đúng source/version/chunk/span và
hash excerpt trên PostgreSQL thật. Browser rehearsal kiểm desktop/mobile,
focus citation, Escape và cancel; CSS viewport tương đương zoom 200% không
phải kiểm chứng native browser zoom. Demo local dùng shared state memory;
Helm mới chỉ render/validate, chưa deploy Kubernetes, Redis chaos hay TLS.

Backup/restore PostgreSQL riêng đã đạt: 27 public tables khớp full-row hashes,
counts và normalized schema; migration head, 200 products/1.773 reviews,
20 documents/chunks/vectors cùng catalog/corpus/index pins giữ nguyên.
Dump 13.665.663 bytes, SHA256
`2dfe46e8e1c1387cef499057fa2be171f47cbade22f5d1c92408df9f4a35324c`.
Manifest giữ cả raw schema hashes, failure ban đầu và khác biệt cast tương
đương do PostgreSQL deparser; không bỏ CHECK constraints khi so sánh.
Target `review_backup_20261003` là database riêng; source không reset.
Artifacts ở `output/review-remediation/postgres-backup-restore-verification.json`,
`review-backup-20261003.dump`, `recorded-demo.html` và
`recorded-demo-verification.json`. Bản RECORDED có 6 public results đã đạt
strict gate, desktop/mobile không overflow, console/page/network errors bằng 0.

P7/P8 successor **chưa chạy**: automatic approval review chặn lệnh P7 trước
dispatch vì cần chấp thuận cụ thể cho evaluation payload tới provider.
`provider-consent-manifest.v1.json` ghi HTTPS OpenAI endpoint, các lớp dữ liệu
public/synthetic và cost caps. Không dùng source/test/demo gate làm quality
report; cần hoàn thành lifecycle successor ở trên sau khi được phép dispatch.

Challenge mới có 20 câu/6 strata và 5 evaluator probes tại
`evaluation/v3/review-challenge-v1/`. Canonical packet hash
`f1e13fb4643c3f16f406f49e3fa7955c5943ef878d7e8da644195990b5d4ba71`;
operator phải lock trước request đầu và không sửa source/oracle theo output.
Codex soạn từ source records mà không đọc gold conversation hay SUT answers;
đây là engineering challenge có biết review, chưa phải externally untouched
test set. Human audit 12 mẫu/6 strata và 5 probes vẫn pending.

Independent automated oracle audit đã kiểm 20 cases, 5 probes, 78 source facts,
72 spans và 20 review records, không thấy serious defect; 16 cases accept và
4 accept-with-uncertainty (`c08,c09,c15,c16`). Artifact cùng
`grading-policy-lock.v1.json` ghi scope metadata của review và requested
semantic clauses trước SUT: chi tiết bổ sung trong source inventory không
trở thành yêu cầu trả lời. Không sửa packet theo output. Đây là Codex/gpt-6
audit trong cùng project/model family, không phải human/external validation.
Template 12 mẫu + 5 probes để người thật chấm đã chuẩn bị, verdicts đều pending.

Tải 10/100/500 users, multi-replica, TLS/public deployment, human adjudication
và manuscript/slides chưa được xác minh trong remediation này. Không suy ra các
claim đó từ unit tests hoặc single-user rehearsal. Không cần mở rộng kiến trúc
để hoàn thành scoped thesis/demo gates.
