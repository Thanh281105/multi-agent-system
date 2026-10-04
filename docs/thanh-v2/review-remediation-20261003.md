# Remediation review trước bảo vệ — 03/10/2026

Audit baseline: `42311032c3ed59147ae19e6f3e5727cee0eff93b`, nhánh `thanh-v3`.
Các sửa đổi được chia theo issue và phải có regression cùng evidence tương ứng.
Không sửa snapshot, gold, protocol hay report đã freeze tại chỗ.

Cập nhật 04/10/2026: P7 review v5 hoàn tất 36/36 trên source `779d607`.
P8 review v6 kết thúc với **708/720 completed, 12 failed**, native exit 3;
chưa đạt gate đầy đủ và chưa chạy calibration, held-out judging hay publication.
Hai sửa lỗi mới `d8d31bd` và `4cba667` làm các lượt đó thành evidence lịch sử.
Source mới cần freeze, P7 mới và full P8 mới; không carry 708 kết quả hoặc chỉ
gọi lại 12 ca lỗi. Regression planner/supervisor/continuation mục tiêu đạt
128 tests bằng model giả/off, không DB hoặc provider; đây không phải SUT quality.

Source gate trước followup E5: Ruff/format và mypy đạt; full offline suite trên PostgreSQL fixture
riêng đạt **1.084 tests**, 27 integration tests deselected (858,24 giây).
JUnit: `output/review-remediation/offline-tests.xml`. Frontend clean install
từ lockfile, lint, 101 tests và production build đạt. Helm production/kind
rendering cùng negative pin validation đạt; strict smoke có thêm 8 tests đạt.

Source gate trước followup đọc checkpoint, trên commit `04e61e6`: **620 tests đạt**, 25 integration tests
deselected, không failure/error/skip (480,09 giây). Đây là 41 modules evaluator
và runtime bị ảnh hưởng, không phải full-suite rerun 1.084 tests. Ruff đạt,
286 files đúng format và mypy 171 source files đạt; 289 tracked source/test
hashes giữ nguyên trong suốt lượt chạy. Evidence:
`output/review-remediation/source-checks.v4.json` và
`evaluator-runtime-regressions.v4.xml`. Lượt trước có bốn fake repository
chưa theo API review batch đã được sửa riêng trong `50d9240`; JUnit thất bại
v2 được giữ nguyên, v3/v4 không ghi đè evidence đó.

## Scope và gates

| Issue | Required gate | Trạng thái |
| --- | --- | --- |
| E1 price/rating/pages | Field-aware typed parser + actual catalog regression | Đã sửa parser và alias giá niêm yết, giữ quoted title là literal; provider trên source `4cba667` chưa chạy |
| E2 fresh entities/review | Resolve từng title, ambiguity, fresh compare/review | Đã sửa entity/intent và guard evidence cho mọi product ID được yêu cầu; fresh provider trên source `4cba667` chưa chạy |
| E3 book domain | Book guard cho candidate, aggregate, direct IDs và sandbox | Đã sửa; giữ 200 raw records, 199 sách đủ điều kiện v2 |
| E4 semantic/numeric judge | Independent numeric/adversarial/per-claim evaluator tests | Numeric và contract regressions đạt; 5 semantic diagnostics đạt; independent calibration pending |
| E5 citations/retrieval metrics | Binding/field matching; coverage theo receipt claims; recall có relevance authority | Đã sửa mẫu số/bindings và cơ chế per-claim calibration; calibration thực và full successor pending; document recall chưa đo |
| E6 quality evidence | Absolute/category historical results; corrected successor | Historical tables đã đối chiếu; P8 v6 source `779d607` terminal 708/720 với 12 failed, chưa chấm chất lượng |
| E7 cancel before admission | Stalled first-event/idle timeout + persisted cancel identity | Đã sửa; timeout/controller tests và browser cancel/reconcile đạt |
| E8 deployed runtime | Helm pins + strict conversation/JSON/SSE/knowledge smoke | Helm và local PostgreSQL demo lịch sử đạt; deployed/browser gate trên source mới chưa chạy |
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

Completion audit `output/review-remediation/review-completion-audit.v1.json`
phát hiện preflight v1 dùng tỷ lệ required gold facts có citation thay vì
tỷ lệ receipt claims được chính citation của claim hỗ trợ. Preflight
`source-freeze.v1.json` bind source `e87dcaf`, chưa có cell nào thực thi;
không dùng freeze này cho source sau followup E5. Phải tạo protocol/run/freeze
mới sau source checks. Commit `f125629` đã đổi mẫu số thành toàn bộ receipt
claim records, bind verdict vào chính citation của mỗi claim và parsed model
output, rồi rederive khi replay/join. Không có claim thì coverage là `null`,
không tính thành 100%. Independent audit có 10 assertions Python thuần đạt,
bao gồm artifact đã rehash nhưng sửa output/scores/coverage và legacy hash.
Local tests với verdict được inject chỉ kiểm contract
và aggregation; chúng không chứng minh model judge hiểu paraphrase, phủ định
hay claim ngoài gold. Những phép đo đó vẫn cần diagnostics và calibration
với nhãn từng claim được chấm độc lập trước held-out scoring.

Commit `eb0d59a` thêm reference labels cho từng claim, giữ attribution automated
hay human và case/packet hash. Calibration tính tổng claim verdict bất đồng
chia tổng actual claims, khóa threshold 0,25 trước dispatch; reference thiếu
hoặc không có actual claim không tạo được freeze. Successor judge không dùng
freeze legacy thiếu authority này. Kiểm tra foreign-case đã rehash cũng bị
từ chối. Đây là cơ chế và regression offline; chưa có 32 nhãn/outputs thực để
khẳng định semantic calibration đạt.

Hậu kiểm còn tìm và sửa hai boundary defects: commit `555cf83` lưu native
development case gắn với independent reference, rồi validate/rebuild toàn bộ
record trước freeze; commit `8f10f83` kiểm contract/schema của mọi answer trong
packet trước direct invocation, ledger key, scope hay reservation. Independent
re-audit có 9 assertions cho freeze và 18 cho preflight đạt: artifact đổi
opaque answer ID, own citation hoặc case bị từ chối sau rehash; mixed packet
valid-first/mismatch-later cũng bị chặn với số dispatch bằng 0. Các checks
này dùng Python thuần, không phải semantic outputs của provider thật.

Commit `04e61e6` sửa ledger judge vốn chỉ nhận protocol historical: CLI truyền
`SuccessorBindingV3` đã validate vào cả development và held-out runners. Runner
đối chiếu protocol/repeat/gold/split cùng successor schema; journal key bind
binding SHA. Không có authority này thì hash mới vẫn bị từ chối. Independent
re-audit có 26 assertions đạt; actual v23 journal headers giữ payload/hash.

Ảnh review performance được đối chiếu riêng trong
[8 giả thiết và cách xử lý](performance-review-20261003.md). H3/H7 và bốn
cancellation SQL preflights còn sót của H5 đã sửa; H1/H2/H4 trùng mô tả E11,
H5 trùng E10. Runtime tiếp tục bounded sequential; chưa đo provider p95 hay
tải nhiều users để quyết định thay scheduler.

## Demo rehearsal lịch sử — trước source followup

Các dấu kiểm dưới đây mô tả capture trước các sửa lỗi tiếp theo. Chưa có
deployed/browser rehearsal trên source `4cba667`; không suy ra pass hiện tại
từ frontend bytes giữ nguyên hoặc từ health check.

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

Sau khi user chấp nhận chạy, P7 successor trên source `d015ee7` hoàn tất
36/36 và native repeat decision chọn 3. P8 cùng source ghi nhận 194/720
completed trước khi tiến trình và Docker không còn chạy; chưa xác định nguyên
nhân dừng. Một cell đang chạy chưa có durable result được seal ambiguous,
525 cell còn pending. Checkpoint gốc được giữ riêng; không gọi lại cell
ambiguous hoặc dùng partial run để chấm chất lượng. Native partial report ở
`output/evaluation-v3/review-heldout-v3/partial-execution-report.p8.json`.

Native ledger chuyển duy nhất reservation hết hạn 7.416.750 nanoUSD sang
unknown, không giảm encumbered cost hay tăng cap. Sau đối soát, active và
reserved đều bằng 0; known là 42.403.715.930, unknown là 482.974.590 nanoUSD.
Đây là ledger estimate, không phải hóa đơn provider. Năm evaluator diagnostics
đã hoàn tất riêng, không thay thế calibration hoặc evidence P7/P8.

Hậu kiểm lượt partial phát hiện CLI đọc checkpoint có thể thực thi các cell
pending bằng forbidden factory rồi ghi failed. Commit `0f49e3a` chuyển ba
đường đọc partial report, complete P7 và complete P8 sang replay thuần, giữ
nguyên bytes trước khi kiểm coverage. Regression kiểm pending, orphan, sealed
ambiguity và receipt sai provenance; full 720-cell test dùng executor fake,
không phải live P8. Sau source followup này phải freeze/rerun phiên bản mới; P7/P8
`d015ee7` vẫn là bằng chứng của source cũ. Không dùng source/test/demo gate
làm quality report; chỉ vào operate khi native coverage đủ 720/720.

Theo phạm vi user xác nhận, hai container PostgreSQL của repo được dừng khi
không có job dùng chúng; Docker/WSL và các dịch vụ repo khác giữ nguyên.
Các capture demo/backup trên là evidence rehearsal tại thời điểm ghi;
không khẳng định endpoint hay database đang chạy hiện tại.

Challenge mới có 20 câu/6 strata và 5 evaluator probes tại
`evaluation/v3/review-challenge-v1/`. Canonical packet hash
`f1e13fb4643c3f16f406f49e3fa7955c5943ef878d7e8da644195990b5d4ba71`;
operator phải lock trước request đầu và không sửa source/oracle theo output.
Codex soạn từ source records mà không đọc gold conversation hay SUT answers;
đây là engineering challenge có biết review, chưa phải externally untouched
test set. Human audit 12 mẫu/6 strata và 5 probes chưa có nhãn.
Ngày 04/10 user xác nhận không có human labels, bản thảo hoặc slide để rà soát;
không dùng automated audit thay cho người thật hay tuyên bố đã kiểm tài liệu đó.

Independent automated oracle audit đã kiểm 20 cases, 5 probes, 78 source facts,
72 spans và 20 review records, không thấy serious defect; 16 cases accept và
4 accept-with-uncertainty (`c08,c09,c15,c16`). Artifact cùng
`grading-policy-lock.v1.json` ghi scope metadata của review và requested
semantic clauses trước SUT: chi tiết bổ sung trong source inventory không
trở thành yêu cầu trả lời. Không sửa packet theo output. Đây là Codex/gpt-6
audit trong cùng project/model family, không phải human/external validation.
Template 12 mẫu + 5 probes để người thật chấm đã chuẩn bị, verdicts đều pending.

20 public challenge đã thực thi trên source `c12a73d`: HTTP 200 và durable
readback đạt 20/20. Chấm tự động theo rubric khóa trước request cho **4 pass,
14 fail, 2 unknown** (`c18,c20` cần phân xử mức đủ của abstention chung chung).
Đây không phải human adjudication hay paired P7/P8 quality evidence. 11 claim
được đối chiếu bằng native source reopening; hai claim demo price của `c06`
được đối chiếu riêng với original durable catalog-search tool evidence cùng
owner, version và content identities. Không dùng giá hiện tại thay nguồn gốc,
không gọi đó là benchmark sandbox reset proof. Claim có citation hỗ trợ vẫn
có thể thiếu yêu cầu tác vụ; 15 câu không có claim giữ coverage `null`.
Evidence ở `challenge-actual-quality-audit.v4.json`, bản bổ sung v5 và
`challenge-sandbox-durable-source-capture.v2.json` trong `output/review-remediation/`.
Template human v2 bind đúng packet `f1e13...`, 12 câu đã chọn trước và năm
diagnostic outputs cũ; toàn bộ human labels vẫn `null`. Template v1 có packet
binding cũ được giữ lại, không dùng làm human proof.

Challenge tái hiện biến thể còn sót trong E1/E2: lời dẫn hoặc câu theo sau bị
giữ trong catalog query, “điểm từ 4,8 trở lên” và “ba cuốn” chưa được nhận,
tên chứa “Tiểu Sử” hoặc cụm “điểm đánh giá” tạo expert obligations thừa.
Commit `868018f` sửa parser các trường hợp này, phân biệt yêu cầu review thật
với câu phủ định dùng review, nhận câu hỏi tác giả và giữ bound tối đa năm sách.
Regression dùng catalog snapshot thật trong SQLite fixture và provider giả;
không hardcode đáp án hoặc đổi source records/oracle theo output.

Một counterexample grounding riêng cũng được tái hiện: câu giới hạn “does not
establish ... edition” khiến cả work claim khẳng định bị `negation_mismatch`.
Commit `c441798` xét polarity theo từng câu, chuyển nguồn có polarity hỗn hợp
sang semantic verification; claim sai vẫn bị semantic verdict từ chối.
Số nằm trong đúng literal cited title có chữ được phân biệt với quantity;
số trang/giá hay số ngoài nguồn vẫn cần typed facts hoặc bị chặn. Không nới
ACL/provenance và không dịch hoặc viết lại nguồn. Grounding/answer regressions
đạt 44 tests bằng provider giả; chưa chứng minh actual model outputs sau sửa.

Source followup làm challenge `c12a73d` và các rehearsal cũ thành evidence
lịch sử. Không ghi đè chúng hoặc gọi challenge đã dùng để sửa là untouched.
Gate v6 giữ nguyên bằng chứng 294 pass/4 fail: parser đã loại cụm yêu cầu
knowledge cùng catalog query, ảnh hưởng cả continuation và no-RAG. Commit
`0c584b5` chỉ loại entity riêng khỏi intent, giữ câu hỏi knowledge chung và
context của “cuốn này”; 31 regression mục tiêu qua bằng provider giả.
Review grounding độc lập phát hiện heading Markdown làm nguồn chỉ có câu
phủ định mất deterministic guard. Commit `69b3fab` bỏ heading khỏi polarity;
25 grounding regressions và 18 probe độc lập qua, không gọi provider/DB.
Các gate riêng này không thay joint gate trên source đã commit hoặc chất
lượng trả lời thật sau sửa; giữ cả capture đỏ và xanh để truy vết.
P7 v4 không thực thi. User sau đó xác nhận 36 P7 + 720 P8 bổ sung: P7 v5
hoàn tất 36/36 trên `779d607`, repeat decision chọn 3. P8 v5 dừng ở 550
completed, một ambiguous và 169 pending khi Windows ghi sự kiện tắt máy;
không suy diễn ai gây ra hoặc sửa checkpoint. P8 replacement v6 được khởi
chạy trong phạm vi goal tại thời điểm đó và kết thúc như ghi bên dưới.
Các lượt này không còn là nguồn evidence của source mới; 32 development
calibration + 720 held-out judgments chưa sử dụng. Một batch SUT mới phải
có chấp thuận số lượt bổ sung; không gọi lại cell ambiguous, 20 challenge
hoặc năm diagnostics đã hoàn tất.

P8 v6 ghi 720 started, 708 completed, 12 failed, không pending/ambiguous;
12 lỗi đều thuộc `held_shopping_merchant_05` qua bốn variants và ba repeats.
Durable catalog step có evidence hợp lệ cho product 141 nhưng thiếu 158;
assessment cũ vẫn báo catalog fulfilled, khiến answer preparation ném
`required_model_evidence_unavailable` trước `answer.draft`. Không có lỗi
category trống trên capsule native. Phân tích chỉ dùng request và step
evidence đã lưu, không gọi provider hoặc chỉnh gold/receipt.

Commit `d8d31bd` nhận “chuyển mức niêm yết … về …” ở ngoài quoted titles;
shopper được chặn trước model/tool, merchant chỉ tạo proposal với target đã
resolve và vẫn cần confirmation. Literal đổi giá trong tên sách không biến
thành action hay lọt thành mức giá proposal. Commit `4cba667` yêu cầu mỗi
product ID explicit có candidate và fact gắn CATALOG reference; thiếu sách
thì dừng trước synthesis với `evidence_incomplete`. Native full supervisor
regressions tái hiện sáu lỗi trên guard cũ và đạt 11/11 trên guard mới;
không nới GroundingVerifier hay bỏ nghĩa vụ evidence.

Terminal audit `p8-successor-v6-terminal-audit.v1.json` ghi SUT known cost
$1.33538417, khớp delta ledger; unknown delta bằng 0. Canonical snapshot sau
terminal: known $45.15622668, unknown $0.48297459, reserved/active bằng 0,
remaining $54.36079873 trong cap $100, max concurrency 2. Đây là estimate
từ durable usage, không phải hóa đơn. Không export reference, calibrate,
judge hoặc gọi `operate` trên P8 v6; native partial report exit 3 được giữ
nguyên. Các số ledger challenge bên dưới là snapshot trước P7/P8 này.

Ledger sau challenge ghi thêm 85.465.810 nanoUSD: 50 attempts, 49 success và
một error đã biết usage, một retry. Known tổng là 42.489.181.740, unknown giữ
482.974.590 nanoUSD, reserved/active bằng 0; còn 57.027.843.670 nanoUSD trong
cap $100. Đây là pricing estimate từ durable usage, không phải hóa đơn.
Snapshot đối soát ở `output/review-remediation/challenge-ledger-audit.v1.json`;
operator phải kiểm lại trước dispatch mới, không tạo account hoặc tăng cap.

Tải 10/100/500 users, multi-replica, TLS/public deployment, human adjudication
và manuscript/slides chưa được xác minh trong remediation này. Không suy ra các
claim đó từ unit tests hoặc single-user rehearsal. Không cần mở rộng kiến trúc
để hoàn thành scoped thesis/demo gates.
