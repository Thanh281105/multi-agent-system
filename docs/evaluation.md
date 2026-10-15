# Phương pháp evaluation

## 1. Câu hỏi nghiên cứu và giới hạn

**Câu hỏi chính hiện tại (đang là draft):** trên cùng task, snapshot, nguồn,
model và ngân sách, `Adaptive MA + RAG` của `app/v2` có tăng tỷ lệ câu trả lời
vừa đúng vừa được nguồn hỗ trợ so với `Single-Agent + RAG` hay không? Đóng góp
được kiểm tra là gói điều phối adaptive có giới hạn; không tách riêng và tuyên
bố đã chứng minh từng specialist hoặc từng stage.

Các đối chiếu phụ là `Fixed MA + RAG` so với SA, `Adaptive MA + RAG` so với
Fixed MA để ablate gói adaptive planning/continuation, và Adaptive MA có/không
RAG. Câu hỏi dưới đây là lịch sử của evaluation cũ; chúng không được xem như
các giả thuyết đã được P8 kiểm định:

1. Bốn specialist book agents có cải thiện frozen task rubric so với trợ lý
   product-only trên cùng case/repetition không?
2. Model-assisted routing, planning, specialist reasoning và synthesis đóng góp
   gì khi bỏ riêng từng stage?
3. Hệ thống giữ nhãn thế nào trước typo, paraphrase, distractor và prompt
   injection?
4. Đổi lại bao nhiêu latency, token và estimated cost theo pricing đã pin?

Rubric đo routing, authorized plan, structured answer assertions và retrieval
trên snapshot lịch sử. Nó không đo toàn bộ chất lượng ngôn ngữ tự do, human
preference, dữ liệu Tiki hiện tại hoặc khả năng tổng quát hóa ra marketplace.
Regression 100% không có nghĩa “AI chính xác 100%”.

Repository có ba lane cần giữ tách biệt:

| Lane | Vai trò | Trạng thái |
| --- | --- | --- |
| v1 scripted/reference | Compatibility regression trên catalog/review snapshot | Đã có evidence lịch sử |
| v2 paired | Historical deterministic/model-assisted comparison với `tiki_books_vi_28_v1` | Đã có protocol/capture lịch sử; không dùng làm P7/P8 result |
| v3 Package 7/8 | Frozen corpus/evaluation split, pilot và held-out benchmark | P7/P8 r7 đạt 36/36 và 720/720 trên `app/v2`, nhưng chỉ là development trace; chưa có judge/operate. Các P8 judge runs cũ là historical. Xem §12.3–12.5. |

## 2. Frozen inputs và provenance

- Gold cases: [`evaluation/cases.v1.json`](../evaluation/cases.v1.json)
- Robustness corpus: [`evaluation/corpus.v2.json`](../evaluation/corpus.v2.json)
- Paired experiment: [`evaluation/experiment.v2.json`](../evaluation/experiment.v2.json)
- Bounded live configuration: [`evaluation/experiment.live-pilot.v2.json`](../evaluation/experiment.live-pilot.v2.json)
- Pricing manifest: [`evaluation/pricing/openai-standard-2026-08-25.v2.json`](../evaluation/pricing/openai-standard-2026-08-25.v2.json)

Historical v2 dataset là `tiki_books_vi_28_v1`: 28 clean cases và 16
label-preserving transformations, tổng 44 correctness cases. Bốn transform type
(`typo`, `paraphrase`, `distractor`, `injection`) có bốn case mỗi loại.

Snapshot binding:

| Artifact | Frozen value |
| --- | --- |
| Snapshot path | `data/snapshots/tiki-books-v4-eval` |
| Products / reviews | 200 / 1.773 |
| Cases SHA-256 | `1838b7c5f3d43ef07e3595fcc507f755668b6330ff2d18bf9d7a8902fd2a2650` |
| Canonical v2 dataset SHA-256 | `92974752db70477761578b11c1f7d98c7456a7fe1f2d64c6fef51612cda370e5` |
| Snapshot SHA-256 | `986803ba95d268cf158f36103efa2e1ce00c6134b0e03b67d96c7058f019d66d` |
| Manifest SHA-256 | `e4f6580e33aa458713842855a7bb58b0e9a73942d888ff124ba4d30fd6513e8e` |
| Quality report SHA-256 | `b75c8e0f6efb8da8278b4d3babd33ffb9fabdcf5b58fea5678be90de4c537eac` |

Gold labels không được tính lại từ SUT output trong lúc benchmark. Protocol còn
ghi experiment/corpus/evaluator/pricing hashes, Git revision/dirty state, network
policy, model bindings, retrieval backend, schedule seed và execution budget.

V3 gold/split hiện hành là [`evaluation/v3/gold.v3.json`](../evaluation/v3/gold.v3.json)
và [`evaluation/v3/split.v3.json`](../evaluation/v3/split.v3.json): 80
conversation, gồm 20 development và 60 held-out. Gold origin là
`automated_pre_sut_spec`; `human_author_ids` và `human_judge_ids` đều rỗng.
Benchmark Q/A, split, calibration labels và judge artifacts không được đưa vào
published v2 corpus/index.

## 3. Legacy v2 variant hierarchy

| Variant | Parent | Mục đích |
| --- | --- | --- |
| `deterministic_book_catalog_v2` | — | Baseline product-only deterministic |
| `deterministic_v2` | product-only baseline | Thêm Review, Trust, Market specialists, không model calls |
| `hybrid_full` | `deterministic_v2` | Routing + planning + specialist + synthesis bằng model khi hợp lệ |
| `hybrid_no_router` | `hybrid_full` | Cô lập model routing |
| `hybrid_no_planner` | `hybrid_full` | Cô lập model planning |
| `hybrid_no_specialist` | `hybrid_full` | Cô lập specialist model selection |
| `hybrid_no_synthesis` | `hybrid_full` | Cô lập model synthesis |

Full experiment pin model snapshot theo stage, reasoning effort, provider
policy, request timeout, retry, max output tokens, concurrency và circuit
breaker. Credential không được ghi hoặc hash vào artifact.

Khi dùng `--variant`, runner tự đóng dependency chain. Chọn
`deterministic_v2` kéo thêm `deterministic_book_catalog_v2`; chọn `hybrid_full`
kéo thêm cả hai deterministic ancestors. Vì vậy một live smoke hybrid hiện tại
chạy ba variants, không phải hai.

## 4. Legacy v2 paired protocol

Full measured matrix:

- correctness: `44 cases × 3 repetitions × 7 variants = 924` observations;
- latency: `7 cases × 5 repetitions × 7 variants = 245` observations;
- tổng measured: `1.169` observations;
- warmup: một turn mỗi variant, tổng 7, bị loại khỏi measured matrix;
- schedule seed `42`; clustered bootstrap `10.000` samples.

Trong từng case/repetition, variant order được seeded-shuffle rồi interleave.
Repetitions được gom thành case mean; bootstrap cluster theo independent base
case, không giả định mỗi repetition là một sample độc lập.

Runner mặc định chặn network. Hybrid cần explicit `--allow-network`. Dirty
worktree bị từ chối; `--allow-dirty` chỉ dành cho thăm dò và artifact vẫn ghi
`git_dirty=true`, nên không dùng nó cho evidence chính thức.

Bundle được ghi qua staging + atomic rename và không overwrite output có sẵn.
`manifest.json` khóa hash/size/count của protocol, observations, comparisons,
omissions, robustness, pricing và report. Validator kiểm tra exact observation
coverage, sequence, pricing, paired comparisons, omission policy, bootstrap và
robustness; omission hợp lệ duy nhất cho analysis cell không có pair là
`no_comparable_paired_values`.

## 5. Metrics

| Metric | Direction | Ý nghĩa |
| --- | --- | --- |
| `task_success` | Cao hơn tốt hơn | Status hợp lệ và mọi critical assertion pass |
| `routing_correct` | Cao hơn tốt hơn | Intent thuộc accepted set |
| `exact_plan` | Cao hơn tốt hơn | Action multiset đúng |
| `answer_assertion_accuracy` | Cao hơn tốt hơn | Structured assertions pass |
| `retrieval_f1` | Cao hơn tốt hơn | Frozen relevant product IDs |
| `end_to_end_latency_ms` | Thấp hơn tốt hơn | Toàn turn orchestration |
| `total_tokens` | Thấp hơn tốt hơn | Provider usage toàn stage |
| `estimated_cost_usd` | Thấp hơn tốt hơn | Usage × pinned pricing |

Delta luôn là `candidate - baseline`; metric direction quyết định win/loss.
Report gồm absolute means, paired mean/median delta, case-level win/tie/loss,
95% clustered bootstrap interval, effect size và sign test khi áp dụng được.
`N/A` không được đổi thành 0; excluded pairs và omissions phải hiện rõ.

Robustness ghép transformed case với clean parent cùng repetition và báo task
success, intent consistency cùng degradation. Đây là invariance test trong
frozen corpus, không phải security certification.

## 6. Chạy deterministic paired v2

Đây là đường offline chính, không cần provider key:

```powershell
python -m app.evaluation.v2_runner run `
  --experiment evaluation/experiment.v2.json `
  --variant deterministic_v2 `
  --run-id run_books_deterministic_v2

python -m app.evaluation.v2_runner validate `
  --bundle output/evaluation-v2/run_books_deterministic_v2

python -m app.evaluation.v2_runner compare `
  --bundle output/evaluation-v2/run_books_deterministic_v2 `
  --baseline deterministic_book_catalog_v2 `
  --candidate deterministic_v2 `
  --metric task_success `
  --phase correctness
```

Dependency closure tạo hai variants, nên expected measured matrix là
`44 × 3 × 2 + 7 × 5 × 2 = 334` observations. Output dưới
`output/evaluation-v2/` bị ignore; chỉ freeze bundle sau khi đã review revision
sạch, protocol hash, coverage, omissions và artifact hashes.

## 7. Live/hybrid run

Chạy full seven-variant experiment chỉ từ clean revision và với explicit đồng ý
network/cost:

```powershell
python -m app.evaluation.v2_runner run `
  --experiment evaluation/experiment.v2.json `
  --allow-network `
  --run-id run_books_hybrid_v2
```

Bounded wiring smoke:

```powershell
python -m app.evaluation.v2_runner run `
  --experiment evaluation/experiment.live-pilot.v2.json `
  --variant hybrid_full `
  --max-cases 1 `
  --allow-network `
  --run-id run_books_live_smoke_v2
```

Repository **không có checked-in live-LLM result cho corpus Tiki Books hiện
tại**. Live pilot số liệu cũ chạy trước khi thay corpus/baseline là evidence
wiring generic lịch sử, không được dùng cho current Tiki Books claim. File
`experiment.live-pilot.v2.json` chỉ là cấu hình có thể tái chạy, không phải kết
quả.

## 8. Source-current successor và kết quả review lịch sử

### Successor trên source đã remediation — 2026-10-05

Source `74149c8bc395852772e8b9ef92853e0b914ed816` hoàn tất P7 v15 `36/36` và P8
v16 `720/720`. Validation độc lập đọc lại native publication và xác nhận report
SHA-256 `97cf42490819abdb55f61173d178701760b826f26ace7f74e4a3cc3a9b042df9`;
artifact local là
[`current-publication-validation.v8.json`](../output/review-remediation/current-publication-validation.v8.json).
P8 gồm 60 conversations, bốn variants × 180 cells, ba repeats và 27 work groups;
752 lượt chấm tự động (32 calibration, 720 held-out) hoàn tất, 0 lỗi và 0 job mơ hồ.
720 cells là coverage của execution, không phải 720 mẫu độc lập hay 720 câu đúng.

| Variant | Task completion / 180 | Mean latency / cell | SUT cost / 1.000 cells |
| --- | ---: | ---: | ---: |
| SA + RAG | 42 (23,3%) | 3657 ms | `$2,00` |
| Fixed MA + RAG | 41 (22,8%) | 2346 ms | `$1,21` |
| Adaptive MA + RAG | 41 (22,8%) | 5053 ms | `$2,89` |
| Adaptive MA, no RAG | 9 (5,0%) | 1715 ms | `$1,09` |

Task completion theo category, vẫn là kết quả của automated judge:

| Category | SA + RAG | Fixed MA + RAG | Adaptive MA + RAG | Adaptive MA, no RAG |
| --- | ---: | ---: | ---: | ---: |
| Multi-constraint | 0/36 | 0/36 | 0/36 | 0/36 |
| Knowledge/source | 33/36 | 32/36 | 33/36 | 0/36 |
| Multi-expert compare/recommendation | 0/36 | 0/36 | 0/36 | 0/36 |
| Multi-turn memory | 0/24 | 0/24 | 0/24 | 0/24 |
| Insufficient/conflict/injection | 9/24 | 9/24 | 8/24 | 9/24 |
| Shopping/merchant | 0/24 | 0/24 | 0/24 | 0/24 |

Paired estimates use a 10.000-resample percentile bootstrap over 27 work groups;
cell-weighted rates and work-group-weighted effects therefore differ:

- Adaptive MA + RAG vs SA + RAG: `−0,31` percentage points, 95% CI
  `−0,93` to `0,00`. The run shows no task-completion gain for MA; it does not
  establish equivalence on other quality measures.
- Adaptive MA + RAG vs adaptive MA without RAG: `+15,43` points, 95% CI
  `+7,41` to `+24,69`.

P8 accounting records `$4.64562124` known cost plus `$0.052923` unresolved
reservation estimate, for `$4.69854424` effective report cost. This is ledger
accounting, not provider invoice verification. Document recall is unmeasured in
all 720 cells. Human-author and human-judge IDs are empty; there is no human
adjudication. Held-out cases were reused during engineering, so this is not an
untouched challenge set. The paired confidence intervals do not remove these
limitations.

The source gate separately reports 610 unique test cases across 32 modules, with
Ruff, format and mypy passing. This is source evidence, not live answer-quality
or deployment evidence. The current strict HTTP/JSON/SSE/cancel demo plan remains
`prepared_only_not_executed`; do not present it as a passed rehearsal.

### Review 03/10/2026: historical P8 v23 results

P7 v18/P8 v23 below are **historical evidence** and do not validate source
remediation. Their artifacts and old scores are retained for provenance. The
current successor above must be used for source-current claims.

Đã đối chiếu `judgments.json`, `unblinding_key.json`, `preparation.json` và
`analysis.json` của v23. **720/720 là receipt coverage, không phải accuracy.**
Scorer lịch sử có lỗi fact matching và citation representation; các điểm dưới
đây mô tả artifact cũ, chưa là semantic correctness đã được xác nhận.

| Variant | Task successes / cells | Cell rate | Work-cluster mean |
| --- | ---: | ---: | ---: |
| `sa_shared_tools_rag` | 9/180 | 5,00% | 3,0864% |
| `ma_fixed_rag` | 9/180 | 5,00% | 3,0864% |
| `ma_adaptive_rag` | 9/180 | 5,00% | 3,0864% |
| `ma_adaptive_no_rag` | 12/180 | 6,67% | 4,9383% |

| Category | SA RAG | Fixed MA RAG | Adaptive MA RAG | Adaptive MA no-RAG |
| --- | ---: | ---: | ---: | ---: |
| Multi-constraint | 0/36 | 0/36 | 0/36 | 0/36 |
| Knowledge/source | 0/36 | 0/36 | 0/36 | 0/36 |
| Multi-expert compare/recommendation | 0/36 | 0/36 | 0/36 | 0/36 |
| Multi-turn memory | 0/24 | 0/24 | 0/24 | 0/24 |
| Insufficient/conflict/injection | 9/24 | 9/24 | 9/24 | 9/24 |
| Shopping/merchant | 0/24 | 0/24 | 0/24 | 3/24 |

Cell means và work-cluster means dùng weighting khác nhau. Có 60 conversations,
27 work groups, ba repeats; 720 cells không là 720 independent samples.
Adaptive RAG so SA có task delta 0 với 95% CI [0; 0], latency delta
2.509,97 ms với CI [1.301,33; 3.822,61]. Chưa chứng minh quality gain của MA/RAG;
CI task delta 0 không chứng minh tương đương về mọi khía cạnh chất lượng.

Judges là automated model judges; human author/judge IDs rỗng. Calibration
32 measurements từ tám pilot conversations × bốn variants không là 32 tác vụ
độc lập hay human validity. Held-out đã được dùng trong successor/debug, cần
công bố engineering reuse và một challenge set mới khóa sau source freeze.
Human stratified audit và second-judge sensitivity là các evidence gates riêng,
không được tự gắn nhãn human cho kết quả tự động.

V23 có 345 successful knowledge calls trên 267 observations nhưng knowledge
chưa đi vào final evidence/citations của các observation đó. Calls/enabled flag
không chứng minh RAG contribution; phải trace persisted tool results, retrieval
và final grounding. `document_recall_authority=resolved_turn_result_citations_only`
trong report cũ chỉ đo citation proxy, chưa là retrieved-document recall.

## 9. Package 7 historical protocol và successor pilot

**Cập nhật sau P02:** xem [handoff hiện hành](thanh-v2/p02-successor-handoff.md).
Protocol `c8a4…` và v4 artifacts bên dưới là lịch sử trước sửa P02. P7 v5
(`28621f…`) và v8 (`a249d0…`) cũng là lịch sử. Chẩn đoán `operate` v11 cho
thấy JSON schema cho phép cả chín evaluation metric trong khi semantic validator
chỉ nhận sáu. Commit `d030f9a` giới hạn metric enum; protocol
`36ac8fec094b201345d74a324aa569057cb1dae5c506cd78763ee327ad5f983c` gắn với
P7 v10 và là lịch sử. P7 v18 bên dưới bind protocol tại ngày 02/10/2026 sau các thay đổi
retry và giới hạn expert ID.
Historical P7 successor `run_p7_successor_v18` completed 36/36 cells with zero
failures and froze 3 repeats. Protocol SHA-256 is
`2d5f2afface0dd5c1337d52d67bf0b3cca6c66328cfbdf71fa77c58659691d23`; its
repeat-decision SHA-256 is
`40ea5ec8a1890c34195b8e3f4fd29935203834985625605af92fa24bbbd8c5cd`. Known P7
cost is `$0.05163060`. P7 v9/v10 and the old
`aaa7026…`/`0b218d30…` preflights are historical.

Historical P8 successor `run_p8_successor_v23` completed all 720/720 SUT cells,
exact evidence resolution, 32/32 calibration records, judge freeze, 720 blind
judgments, result join, report and paired metrics. Schedule SHA-256 is
`f46949cdae870718dd8d2411a343522842eb34c466554e5350ae960ea7e8892e`; calibration
SHA-256 is
`8176e307bd5a34610f2855e71777f46b708058823621120b1ec209993e1f9031`; report
SHA-256 is
`cf772ac876e0a2f69aef78f68ffa729925eb991359b76bc94da4f26fc6787f5d`. Known
provider usage is `$4.90357425`; the published effective cost is `$4.91358675`,
including `$0.01001250` report-level unresolved-reservation estimate. The report
and paired metrics are in
`output/evaluation-v3/heldout-successor-v23/p8-publication/`. P8 v14-v16 remain
historical, including v16's failed calibration threshold.

Historical corrected P7 freeze artifact do operator tạo tại
`output/evaluation-v3/pilot-corrected-v3/`; đây là output local không được commit
vào repository. Historical protocol SHA-256 là
`385ae09e74a7e8e2896b170f9ad3f2549f6f79390e94b3241cd7da0e4b32721f`.
Sau remediation merchant tại `31f3351`, source-bound successor protocol SHA-256 là
`c8a4b4fb912039b93071fa37030cdbb11971cf3a15c65000d7c7e2ddfff79cde`.

V3 có đúng bốn variants:

| Variant | Topology | Planning | RAG |
| --- | --- | --- | --- |
| `sa_shared_tools_rag` | single | shared | bật |
| `ma_fixed_rag` | multi | fixed | bật |
| `ma_adaptive_rag` | multi | adaptive, tối đa một continuation | bật |
| `ma_adaptive_no_rag` | multi | adaptive, tối đa một continuation | tắt |

Split freeze có 20 development cases và 60 held-out cases. Historical corrected
pilot hoàn tất `36/36` terminal cells; repeat decision chọn global `3` repeats
cho held-out matrix. Chi phí pilot thực tế là `0.05332290 USD`; projected
held-out SUT cost là `5.72929200 USD`. Các số này chỉ là historical
budget/projection evidence, không phải benchmark quality result và không thể
được tái sử dụng cho protocol successor.

P7 successor `run_p7_successor_v5` đã hoàn tất `36/36` terminal cells gồm `4`
warmup và `32` measurements, với schedule SHA
`82617ce0e4004fcda0b542a1769de9653be044643f5189a5b3e88fa0aebd2163`. Repeat
decision chọn global `3` repeats; chi phí pilot là `0.05551620 USD` và
projected held-out SUT cost là `5.91510600 USD`. P8 successors dùng đúng
protocol/repeat decision này trong output riêng; không resume hay ghép cells
vào các runs lịch sử.

Historical P7 v9 preflight for `run_p7_successor_v9` produced schedule SHA
`0b218d30ba8262a7e7da6574945a23202c75eaf8676d1051a116aea4312be5dd`. The P7 v9
checkpoint bound protocol `36ac8fec094b201345d74a324aa569057cb1dae5c506cd78763ee327ad5f983c`
and schedule `6817e844d65633eea19d3e57119c8a6cec1dd10464ee3b2da7fa95b86828b75b`.
At that time it was partial (`13/36` completed, `23` failed/missing); four connection-failed
cells made eight unknown provider attempts, and 19 later cells hit the open
circuit. It remains a terminal historical partial; it has no repeat decision.

Remediation P02 hiện hành đọc/ground giá nhiều offer rồi tạo proposal nếu có
target độc lập do server xác định. Thiếu target vẫn trả clarification an toàn.
P7/P8 phải ghi nhận kết quả thực tế theo gold contract; không suy diễn proposal
hay fabricate citation để đạt một trạng thái gold dự kiến.

Semantic scoring được khai báo là automated model judge (`model_judge`) theo
judge configuration/schema đã hash; không có human semantic judge. Successor
citation precision đối chiếu citation với required-fact bindings. Citation
coverage dùng semantic verdict của từng receipt claim với chính citation của
claim; chỉ bước tổng hợp là deterministic, không suy ra support từ receipt
alone. Không có claim thì coverage là `null`.

Successor `document_recall` giữ `null` / `not_measured`: chưa có independent
exhaustive document-relevance gold và toàn bộ retrieved-document set trong
receipt. Required gold source IDs và final citations không thay thế hai đầu
vào này. Calibration phải được freeze trước held-out scoring. V18/v23 giữ
nguyên metric semantics lịch sử; xem caveat citation proxy ở §8.

## 9. Package 8 additive held-out driver

Package 8 chỉ bổ sung các module `benchmark_*.py`. Mỗi P8 run bind chính xác
protocol và repeat decision của P7 tương ứng; historical P7/P8 artifacts chỉ
được đọc như evidence. CLI có các lệnh local `validate`, `dry-run`, `prepare`,
hai lệnh SUT `run`/`resume`, và `operate` cho lifecycle sau khi SUT đã hoàn tất:

```powershell
$p7Output = 'output/evaluation-v3/pilot-successor-v18'
$p8Output = 'output/evaluation-v3/heldout-successor-v23'
$p7RuntimeManifest = 'output/evaluation-v3/current-machine-ledger/runtime-manifest-v6.json'
python -m app.evaluation.benchmark_cli validate `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json"
python -m app.evaluation.benchmark_cli dry-run `
  --run-id run_p8_successor_v23 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json"
python -m app.evaluation.benchmark_cli prepare `
  --run-id run_p8_successor_v23 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json" `
  --pilot-checkpoint "$p7Output/pilot-checkpoint.v3.jsonl" `
  --pilot-schedule "$p7Output/pilot-schedule.v3.json" `
  --output $p8Output
python -m app.evaluation.benchmark_cli run `
  --run-id run_p8_successor_v23 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json" `
  --allow-network `
  --database-url $env:DATABASE_URL `
  --output $p8Output
python -m app.evaluation.benchmark_cli resume `
  --run-id run_p8_successor_v23 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json" `
  --allow-network `
  --database-url $env:DATABASE_URL `
  --output $p8Output
python -m app.evaluation.benchmark_cli operate `
  --run-id run_p8_successor_v23 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json" `
  --pilot-checkpoint "$p7Output/pilot-checkpoint.v3.jsonl" `
  --pilot-schedule "$p7Output/pilot-schedule.v3.json" `
  --allow-network `
  --database-url $env:DATABASE_URL `
  --judge-budget-nano-usd 250000000 `
  --output $p8Output
```

`validate`, `dry-run` và `prepare` là local-only, không provider call.
`prepare` chỉ chấp nhận results không có citation; citation phải đi qua
`operate` để exact evidence được mở lại dưới authorization đã ghi trong receipt.
`run`, `resume` và `operate` chỉ được dispatch live khi operator truyền rõ
`--allow-network` cùng `--database-url`; `operate` còn bắt buộc per-job judge
budget. URL không được ghi vào output. Schedule P8 là exact
`60 × 4 × frozen_repeats` held-out measurements (`480` hoặc `720`), không có
warmup. Checkpoint append-only không được redispatch cell đã settled;
orphan/ambiguous work phải giữ trạng thái partial. `operate` hash-bind
preparation, calibration, journal, judgment và publication; catalog/review
records chỉ được reopen từ source asset hash-pin và receipt authority tương ứng.
Ranked, oversized hoặc provenance không đầy đủ sẽ dừng fail-closed, không dùng
text từ answer hay gold thay thế evidence.

Khi checkpoint terminal nhưng incomplete, lệnh local-only sau tạo hoặc tái xác
thực một record bất biến `partial-execution-report.p8.json`; lệnh không dựng
runtime live, không dispatch provider và không đưa receipt failed vào scoring:

```powershell
python -m app.evaluation.benchmark_cli partial-report `
  --output output/evaluation-v3/heldout-corrected-v3 `
  --run-id run_p8_heldout_corrected_v3
```

Historical held-out SUT `run_p8_heldout_corrected_v3` remains immutable at
`678/720` completed cells and is not current successor evidence. After the
shopper fixture correction, four fresh runs used the frozen P7 v5 protocol and
each reached terminal partial status with zero pending, ambiguous or orphan
work:

| Run | Schedule SHA | Completed | Failed/missing |
| --- | --- | ---: | ---: |
| `run_p8_successor_v6` | `c0c41ae8…` | 718 | 2 |
| `run_p8_successor_v7` | `ad5b8d57…` | 716 | 4 |
| `run_p8_successor_v8` | `7aae9668…` | 714 | 6 |
| `run_p8_successor_v9` | `9efb06ceb5843f069f1e84e6f099ff57c5103b6a0167114db52e09d0bc24b394` | 712 | 8 |

The latest v9 safe failure codes are one `attempt_timeout_limit_exceeded`, two
`expert_selection_not_authorized`, two `model_response_incomplete` and three
`model_response_invalid`. The old merchant fixture-binding failure is absent
after the source fix. Partial reports contain no raw provider payload or
fabricated citation. Historical v6-v9 runtime ledger accounting was
`10.387962730 USD` known and `0.016542090 USD` unknown; these are not v9-only
costs. Append-only checkpoints cannot redispatch terminal cells, so
none of these runs is scored, calibrated, judge-complete or publishable.
No calibration/judge journal, final result or paired metric exists for those
historical v6-v9 runs.

P8 successor `run_p8_successor_v11` later completed all `720/720` SUT cells with
schedule SHA `92b387b2d453042e4e17ccedc3e914bc9f9ebd9535cf17d7bbfbd14595f1499e`;
failed, missing, ambiguous, pending and orphan counts were all zero. Its
`operate` reached exact evidence preparation but did not freeze calibration:
four model responses failed semantic schema validation, then 28 jobs were
stopped by circuit breaker. A one-case diagnostic identified non-semantic
metric values admitted by the old provider JSON schema. The corrected schema
has a new protocol hash, so v11 judge results are not benchmark evidence; there
is no calibration freeze, blind score, paired metric or publication.

Các failed cells của v9 được ghi lại để truy vết:

| Case | Variant | Repetition | Safe failure code |
| --- | --- | ---: | --- |
| `held_knowledge_source_04` | `ma_adaptive_rag` | 2 | `attempt_timeout_limit_exceeded` |
| `held_multi_expert_09` | `ma_adaptive_no_rag` | 2 | `model_response_incomplete` |
| `held_multi_expert_10` | `sa_shared_tools_rag` | 0 | `model_response_incomplete` |
| `held_multi_expert_10` | `ma_fixed_rag` | 0 | `model_response_invalid` |
| `held_multi_expert_10` | `ma_adaptive_no_rag` | 2 | `expert_selection_not_authorized` |
| `held_multi_expert_12` | `ma_adaptive_rag` | 0 | `expert_selection_not_authorized` |
| `held_shopping_merchant_02` | `sa_shared_tools_rag` | 1 | `model_response_invalid` |
| `held_shopping_merchant_02` | `ma_adaptive_no_rag` | 2 | `model_response_invalid` |

### Source remediation status — 2026-09-23 (historical run snapshot)

Current code makes provider structured-response parse/validation errors
retryable within the frozen `max_retries=1` budget. Expert-selection
schemas now enumerate only fact and evidence IDs present in that request; the
existing local authorization check remains fail-closed. The 18-second provider
attempt cap is unchanged. Regression verification passed `105` tests across
`test_model_runtime.py` and `test_v2_supervisor.py`; Ruff and mypy passed for the
changed modules.

P8 v6-v11 checkpoints remain immutable; none is resumed or joined into a new
run. At the time, the source protocol was
`36ac8fec094b201345d74a324aa569057cb1dae5c506cd78763ee327ad5f983c`. Responses
API payloads were approved. Embeddings API authorization was then pending; the
user approved it on 2026-10-01. That earlier continuation snapshot named P7 v10
and P8 v16; it was superseded by P7 v18/P8 v23 on 02/10/2026, now historical in §8.

### Diagnostic-only replay attempts — 2026-09-23

All eight v9 failed prompts and fixtures were replayed against isolated
PostgreSQL clones. Since the generic runner binds each schedule row to a frozen
P7 pilot case, the five distinct held-out case IDs and workgroups were mapped to
pilot aliases; original user turns, identity and sandbox fixtures, variants,
and repetitions were preserved. Both runs are strictly diagnostic and are not
eligible for P8 scoring. The v9 schedule, checkpoint, and report remain
unchanged.

The first attempt, `run_p8_failed_diag_20260923_a`, ended with `0/8` successful
observations: four cells returned `model_connection_failed`, then four returned
`model_circuit_open`. It recorded zero tokens and zero known cost, with
`0.05446350 USD` left as unresolved reservation evidence. Its manifest names
the isolated database clone `p8_failed_diag_20260923_a`; all eight unresolved
reservation events in its checkpoint are attributed to that diagnostic run.
Attempt B used a separate clone, `p8_failed_diag_20260923_b`, and has zero
unresolved reservations. Clone A is absent from the current Docker
container/volume inventory, so the artifacts identify its ledger but its SQL
settlement record is unavailable for direct inspection. The final reconciliation
places the amount inside the account-level `$0.07100559` carry-forward; it is
already included in the current `$0.47555784` unknown balance and must not be
added again. After explicit user approval to send the held-out prompts and
fixtures to the model provider, the
second attempt, `run_p8_failed_diag_20260923_b`, completed `8/8` observations
with no failed cells. It recorded 26 provider attempts, 48,199 input tokens,
9,617 output tokens, `0.07320495 USD` known cost, and zero unresolved
reservation; every cell remained under the frozen `0.25 USD` cap and total
known cost stayed below the diagnostic `2.00 USD` limit.

Checkpoints and safe summaries are in
`output/evaluation-v3/failed-cell-diagnostic-20260923-a/` and
`output/evaluation-v3/failed-cell-diagnostic-20260923-b/`. The successful
diagnostic artifacts are local-only and excluded from Git. The successful
diagnostic receipts do not repair v9's terminal failures or qualify as official
P8 evidence because the schedule uses pilot aliases. P7 v9 and P8 v12 are
historical. The v10/v16 status described in the earlier ledger snapshot is also
historical; P7 v18 and P8 v23 were the official successors on 02/10/2026. No benchmark
conclusion is drawn from the diagnostic runs.

The checkout `.env` points to SQLite in that snapshot; successor runs use the isolated
PostgreSQL ledger. The 02/10/2026 `codex-p7-p8-ledger` snapshot was `$41.57320211`
known, `$0.47555784` unknown, `$0` reserved/active, and `$57.95124005` remaining
under the `$100` cap. Current SQL scopes account for `$0.40455225` unknown; the
remaining `$0.07100559` is account-level carry-forward without original scope
rows. Diagnostic A's `$0.05446350` is within that carry-forward and belongs to
historical clone `p8_failed_diag_20260923_a`; the original attempt IDs are
unavailable. P7 v18 known cost is `$0.05163060`. P8 v23 known provider usage is
`$4.90357425`; report effective cost is `$4.91358675`, including a
`$0.01001250` report-level unresolved-reservation estimate. The PostgreSQL
account has no active reservation. No P7/P8 Embeddings API attempts were
recorded; all three embedding attempts in the account were for ingestion. The
local-only reconciliation is
`output/evaluation-v3/current-machine-ledger/ledger-reconciliation-20261002-v23-final.json`.
The earlier 2026-10-01 and 2026-10-02 02:07 UTC snapshots remain historical.
Credentials are local-only and excluded from Git.

P7 v9 is a historical partial at `13/36`; P7 v10 and P8 v14-v16 are superseded
successors. Historical P7 v18 froze repeats and P8 v23 completed calibration, blind
judging, report publication and paired metrics.

Các benchmark gates Package 8 đã hoàn tất với P8 v23 tại ngày 02/10/2026;
đây không phải evidence trên source đã sửa sau review:

1. Calibrated automated judge và frozen calibration/configuration bindings.
2. Exact immutable evidence resolver mở lại đúng source/version/chunk/span từ
   durable final `TurnResult`; citation ID hoặc text tự tạo không đủ.
3. Đủ `60 × 4 × frozen_repeats` receipt cells (`480` hoặc `720`), ledger
   attribution hợp lệ, no missing/ambiguous cells, rồi complete
   blind-answer/judgment join.
4. Analysis/report artifact với bindings, counts, paired metrics, omissions và
   partial/complete status được validator chấp nhận.
5. Regression, real PostgreSQL transactional/replay checks, frontend checks khi
   ảnh hưởng, package gate và final documentation review.

## 10. Evidence đã commit

### Current Tiki deterministic regression

[`evaluation/results/reference-v1`](../evaluation/results/reference-v1) là
28-case × 3 deterministic reference hiện hành, gắn đủ snapshot/manifest/quality
hashes và trạng thái `scripted_regression_only`. Tái tạo:

```powershell
ecommerce-evaluate --repeats 3 --output evaluation/results/latest
```

Reference hiện tại đạt frozen routing/plan/assertion/retrieval rubric, nhưng chỉ
là regression trên cùng code/data; latency là local/offline và token/cost là
`N/A`. Paired causal comparison dùng product-only baseline trong v2, không dùng
v1 scripted reference làm single-agent model proxy.

### Legacy generic evidence

Các đường sau thuộc dataset cũ `sample_ecommerce_vi_28_v1`, không phải Tiki
Books hiện tại:

- [`evaluation/results/legacy-reference-v1`](../evaluation/results/legacy-reference-v1)
- [`evaluation/results/baseline-single-agent-v1`](../evaluation/results/baseline-single-agent-v1)
- [`evaluation/results/real-multi-agent-v1`](../evaluation/results/real-multi-agent-v1)
- [`evaluation/legacy/cases.sample-ecommerce.v1.json`](../evaluation/legacy/cases.sample-ecommerce.v1.json)

Hai real-model captures là API captures thật nhưng khác orchestration/prompt và
không paired; delta chỉ descriptive. Chúng ghi `real_model_captured` và
`source-manifest` để audit source SUT, nhưng **không phải external validity** và
không hỗ trợ current Tiki Books live claim.

Chấm lại legacy captures mà không gọi network:

```powershell
python scripts/score_real_baseline.py
python scripts/run_real_multi_agent_benchmark.py --score-only
```

`--score-only` yêu cầu report hiện có và giữ nguyên `sut_source_sha256` cùng
`sut_source_files` của lần capture, vì report đó là provenance của observations
bất biến. Chế độ này chỉ tính lại điểm và report; nó không gắn observations lịch
sử với source checkout hiện tại.

## 11. Điều kiện trước claim mạnh hơn

1. Freeze một complete v2 bundle từ clean revision và validate độc lập.
2. Dùng human/semantic rubric do curator độc lập thiết kế; báo agreement.
3. Replicate qua nhiều seed/model snapshots; không cherry-pick.
4. Báo paired effect/interval/win-tie-loss cùng absolute quality, latency, token
   và estimated/billed cost tách biệt.
5. Thêm load/soak, chaos, drift, fairness, PII/security evaluation trước mọi
   production hoặc marketplace claim.

## 12. Trạng thái Đánh giá Thực nghiệm Hệ thống Lõi

Để đảm bảo tính trung thực khoa học và khả năng kiểm toán của Khóa luận tốt nghiệp (ĐACNTT), các làn đánh giá (evaluation lanes) trong repository được phân định rõ ràng như sau:

### 12.1. Làn Đánh Giá Toàn Trình: Paired v2 Benchmark (Đã Kiểm Toán tại commit `7ae2e18`)
Đây là làn benchmark toàn trình duy nhất có artifact và manifest được ghi nhận chính thức trên source code hiện tại:
* **Bundle Manifest:** [`output/evaluation-v2/run_books_deterministic_v2/manifest.json`](file:///C:/Users/Thanh/Desktop/DACNTT/multi-agent-system/output/evaluation-v2/run_books_deterministic_v2/manifest.json)
* **Quy mô:** 44 cases sách × 3 repeats = 132 cặp quan sát (334 observations tổng cộng).
* **Phạm vi thực thi:** Toàn bộ chu trình từ Router $\rightarrow$ Planner $\rightarrow$ SQL Tools (Database / Catalog / Review / Ranking) $\rightarrow$ Structured Answer Assertions.
* **Kết quả đo đạc:**
  - **`task_success`**: Baseline (Single-Agent Catalog): **56.82%** $\rightarrow$ Candidate (Multi-Agent System): **77.27%**
  - **Mức tăng trưởng:** $\Delta = \mathbf{+20.45\%}$ (Khoảng tin cậy 95% Bootstrap: $[+9.1\%, +34.1\%]$, $p = 0.0039$, **9 thắng, 35 hòa, 0 thua**).
  - **`answer_assertion_accuracy`**: Baseline: **79.28%** $\rightarrow$ Multi-Agent: **84.17%** ($\Delta = \mathbf{+4.89\%}$, **3 thắng, 41 hòa, 0 thua**).

> [!NOTE]
> Kết quả Paired-v2 là bằng chứng thực nghiệm đã được kiểm toán đầu tiên chứng minh kiến trúc Multi-Agent vượt trội Single-Agent trên môi trường đánh giá cục bộ.

### 12.2. Làn Package 7 / Package 8: Kiểm Tra Tính Hợp Lệ Của Kế Hoạch (Plan Admissibility)
Lớp logic lập kế hoạch của hệ thống lõi tại [`app/v2/planning.py`](file:///C:/Users/Thanh/Desktop/DACNTT/multi-agent-system/app/v2/planning.py) đã được nâng cấp (hỗ trợ composite planning, chuẩn hóa chuỗi tiếng Việt, phân giải anaphora, bắt lỗi mâu thuẫn khoảng giá và phòng thủ prompt injection).

Script kiểm tra tính hợp lệ của kế hoạch:
```powershell
python scripts/run_full_p7_p8_benchmark.py
```
* **Phạm vi kiểm tra:** Đo lường **Plan Admissibility** — kiểm tra xem `desired_capabilities` sinh ra từ Planner có thỏa mãn hoàn toàn `action_capability_blueprint` (required, allowed, forbidden) của 80 gold cases (20 Dev + 60 Held-out) và 756 ô lịch trình (P7: 36 ô, P8: 720 ô) hay không.
* **Kết quả Plan Admissibility:** Đạt **100.0% độ hợp lệ kế hoạch** trên cả 3 biến thể có RAG, giải quyết triệt để 4 nhóm lỗi từng đạt 0.0% plan validity trong lịch sử (`multi_constraint`, `multi_expert_compare_recommendation`, `multi_turn_memory`, `shopping_merchant`).
* **Giới hạn học thuật:** Đây là phép đo kiểm tra tính đúng đắn của pha Lập Kế Hoạch (Planning Stage), **chưa phải là phép đo Task Completion toàn trình có Model Judge** của Package 8, do chưa thực thi trọn vẹn specialist execution, live RAG retrieval và automated LLM judging.

### 12.3. Kết Quả Lịch Sử P8 `run_p8_live_v3` (720/720, 2026-10-10)

> **Trạng thái diễn giải cập nhật ngày 12/10/2026:** Đây là kết quả lịch sử của
> `run_p8_live_v3`. Sau run đó, các trace của cùng bộ ca đã được dùng để sửa
> `app/v2/answers.py` và `app/v2/planning.py`; vì vậy số liệu không phải xác nhận
> độc lập cho source hiện tại. Git SHA của SUT ở run lịch sử không được ghi lại.
> Giữ các số judge bên dưới làm chẩn đoán của run cũ, không dùng để khẳng định
> source sau remediation tốt hơn hay kém hơn.

Thực nghiệm toàn trình **Package 8 (P8 Held-out Live v3)** chạy trên 60 ca thuộc split held-out tại thời điểm đó và được tự động chấm bằng **Durable Model Judge V3** với `gpt-5.4-mini`, ghi vào sổ cái PostgreSQL (`codex-p7-p8-ledger`). Sau đó các trace của cùng split được dùng để sửa SUT; trạng thái độc lập đã bị rút lại cho mọi kết luận về source hiện tại.

* **Mã định danh thực thi (Run ID):** `run_p8_live_v3`
* **Quy mô ma trận kiểm thử:** $60 \text{ cases} \times 4 \text{ variants} \times 3 \text{ repeats} = 720$ ô thực nghiệm toàn trình.
* **Tổng số lượt tương tác mô hình (Total Provider Attempts):** 3,530 lượt (2,770 lượt sinh tác tử SUT + 760 lượt thẩm định Model Judge).
* **Độ phủ thực thi chấp nhận kiểm định (Accepted Observations):** **720 / 720 ô (100.0% execution coverage)** — Toàn bộ 720 ô quan sát đều được thu thập đầy đủ vào sổ cái, không có ô nào bị thiếu, hủy bỏ hoặc lỗi mồ côi (lưu ý: đây là độ phủ thu thập dữ liệu thực nghiệm, không phải tỷ lệ hoàn thành tác vụ hay độ chính xác).
* **Chi phí thực thi thực tế (Total Effective Cost):** 9.534 USD.
* **Gói xuất bản kết quả (Publication Artifacts):** [`output/evaluation-v3/heldout-live-v3/p8-publication/p8-report/`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/output/evaluation-v3/heldout-live-v3/p8-publication/p8-report)
  - `report_sha256`: `15a7452b5965e54a80ba47f2b4ecf2eb66b98c20f6ca97fc0cdaa445a523230f`
  - `analysis_sha256`: `97210098587367e1f5661c92b230dcad1be022b2ffad3284b5a29b1ab5bf0fef`
  - `preparation_sha256`: `9e5278559debb684e4b41697221621cf7e3f429ff1fa2547d369bd17b1593ebd`

#### 1. Bảng Chỉ Số Thực Nghiệm Tổng Thể Theo Biến Thể ($N = 180$ ô mỗi biến thể)

| Biến Thể Hệ Thống (Variant) | Task Completion | Claim Support | Valid Plan | Authorization | Mean Repeat StdDev |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **`ma_adaptive_no_rag`** (Baseline No-RAG) | 3.89% (7/180) | 8.89% (16/180) | 23.33% (42/180) | 99.44% (179/180) | 0.008 |
| **`ma_fixed_rag`** (Multi-Agent Fixed RAG) | 33.89% (61/180) | 38.89% (70/180) | **90.00%** (162/180) | **100.00%** (180/180) | 0.094 |
| **`ma_adaptive_rag`** (Multi-Agent Adaptive RAG) | 35.56% (64/180) | **42.78%** (77/180) | 75.56% (136/180) | 86.67% (156/180) | 0.110 |
| **`sa_shared_tools_rag`** (Single-Agent RAG Baseline) | **36.11%** (65/180) | 41.67% (75/180) | 75.00% (135/180) | 87.78% (158/180) | 0.110 |

#### 2. Đối Chiếu Cặp Khoa Học Giữa Các Biến Thể Lõi (Paired Comparative Analysis)

Dựa trên 180 cặp quan sát trực tiếp đối chiếu qua bootstrap clustered analysis:

1. **Hiệu quả sống còn của Retrieval-Augmented Generation (RAG vs. No-RAG):**
   - So với biến thể `ma_adaptive_no_rag`, việc kích hoạt cơ chế RAG giúp **Task Completion tăng vọt từ 3.89% lên 35.56%** ($\Delta = +31.67\%$, $p < 0.001$).
   - Tỷ lệ có căn cứ trích dẫn (**Claim Support**) tăng từ 8.89% lên 42.78% ($\Delta = +33.89\%$).
   - Độ hợp lệ kế hoạch (**Valid Plan**) tăng từ 23.33% lên 75.56% ($\Delta = +52.23\%$).
   - *Kết luận khoa học:* Trong miền nghiệp vụ tư vấn sách đa chuyên gia, mô hình ngôn ngữ không thể giải quyết nhiệm vụ nếu không có tri thức tăng cường từ kho dữ liệu; thiếu RAG dẫn đến sụp đổ gần như toàn bộ (failure rate 96.1%).

2. **Adaptive RAG so với Fixed RAG (`ma_adaptive_rag` vs. `ma_fixed_rag`):**
   - **Task Completion:** `ma_adaptive_rag` đạt 35.56% so với 33.89% của `ma_fixed_rag` ($\Delta = +1.23\%$, 95% CI: $[-1.65\%, +4.32\%]$, 5 thắng, 18 hòa, 4 thua).
   - **Chất lượng Grounding và Bằng chứng Trích dẫn:** `ma_adaptive_rag` vượt trội rõ rệt:
     - **Claim Support:** Tăng trưởng có ý nghĩa thống kê với $\Delta = \mathbf{+4.12\%}$ (95% CI: $[+0.51\%, +8.13\%]$, **8 thắng, 16 hòa, 3 thua**).
     - **Citation Precision:** Vượt trội hơn với $\Delta = \mathbf{+9.34\%}$ (95% CI: $[+3.05\%, +16.80\%]$, **10 thắng, 13 hòa, 4 thua**).
     - **Answerability Abstention & Useful Continuation:** Tăng $\Delta = \mathbf{+18.72\%}$ (95% CI: $[+7.41\%, +31.48\%]$, **8 thắng, 18 hòa, 1 thua**). Cơ chế thích ứng biết chủ động nhận diện ranh giới thông tin để bổ sung dẫn giải hữu ích thay vì từ chối cứng nhắc.
   - **Đánh đổi (Trade-off):** `ma_fixed_rag` duy trì 100% Authorization và 90% Valid Plan nhờ luồng tĩnh nghiêm ngặt, trong khi `ma_adaptive_rag` có độ trễ cao hơn trung bình ~3.8 giây do các bước dynamic re-routing và adaptive query expansion.

3. **Multi-Agent Adaptive RAG so với Single-Agent Shared Tools (`ma_adaptive_rag` vs. `sa_shared_tools_rag`):**
   - **Task Completion:** Tương đương nhau (35.56% vs. 36.11%, $\Delta = -0.55\%$, 95% CI: $[-4.12\%, +3.70\%]$, 3 thắng, 20 hòa, 4 thua).
   - **Chất lượng căn cứ:** Multi-Agent nhỉnh hơn về Claim Support (42.78% vs. 41.67%) và Citation Precision (+0.71%).
   - **Chi phí & Độ trễ:** Single-Agent tiết kiệm hơn (chi phí trung bình thấp hơn 0.0035 USD/cell, độ trễ nhanh hơn 4.0 giây) do không phải chịu chi phí trao đổi và tổng hợp hội thoại liên tác tử (inter-agent synthesis overhead).

#### 3. Phân Tích Độ Bất Định và Tính Nhất Quán Lặp Lại (Repeat Uncertainty)

Đánh giá trên 3 lần lặp lại độc lập cho từng trường hợp trong số 60 held-out cases:
- `ma_fixed_rag`: Đạt **80.0% tỷ lệ đồng thuận tuyệt đối (Perfect Agreement: 48/60 cases)** giữa 3 lần chạy; độ lệch chuẩn trung bình chỉ $\sigma = 0.094$.
- `ma_adaptive_rag`: Đạt **76.7% tỷ lệ đồng thuận tuyệt đối (46/60 cases)**; độ lệch chuẩn trung bình $\sigma = 0.110$.
- `sa_shared_tools_rag`: Đạt **76.7% tỷ lệ đồng thuận tuyệt đối (46/60 cases)**; độ lệch chuẩn trung bình $\sigma = 0.110$.
- Biên độ dao động ngẫu nhiên quanh mức $\sim 0.10$ phản ánh tính ổn định cao của pipeline, loại trừ khả năng kết quả đo đạc bị chi phối bởi tính ngẫu nhiên (temperature noise) của mô hình.

#### 4. Phân Tích Nguyên Nhân Thất Bại (Failure Mode Breakdown)

Trong số các trường hợp Task Completion bằng 0:
- **Claim Support bằng 0:** Xuất hiện ở 103/116 ô task-completion thất bại của `ma_adaptive_rag` (88,8%), 92,4% ở `ma_fixed_rag` và 91,3% ở `sa_shared_tools_rag`. Đây là đồng xuất hiện trong điểm judge, chưa chứng minh nguyên nhân là retrieval thiếu dữ liệu. Trace `held_multi_constraint_02`, repetition 0 cho thấy catalog và knowledge calls đều thành công, citation chứa nguồn Hachette nêu tác giả/chủ đề, nhưng câu trả lời chỉ gọi đó là “work metadata note”; judge chấm task completion và claim support bằng 0. Ít nhất một nhóm lỗi vì vậy nằm ở khâu tổng hợp hoặc trả lời thiếu ý, không phải thiếu tài liệu truy xuất.
- **Từ chối sai mục tiêu (Bad Abstention):** Xuất hiện ở 91.9% số ca thất bại của `ma_adaptive_no_rag`, 63.0% ở `ma_fixed_rag` nhưng giảm xuống chỉ còn 45.7% ở `ma_adaptive_rag`.
- **Plan và authorization theo whitelist của từng case:** `ma_adaptive_rag` có 24/180 ô không đạt metric này (13,3%; bằng 20,7% trong 116 ô task-completion thất bại). Trong 23 ô, trace ghi `product.rank`, `review.compare`, `trust.compare` thay cho các capability bắt buộc `product.catalog.search` và `product.compare`; cùng mẫu này xuất hiện ở 22 ô `sa_shared_tools_rag`. Đây là dấu hiệu planner cho phép chọn template rộng hơn capability scope của case, không phải bằng chứng về lỗi phân quyền tài nguyên hay một lỗi riêng của MA.

##### Ghi chú hậu kiểm trace — 2026-10-12

Các trace của publication `run_p8_live_v3` đã được dùng để xác định remediation trong
`app/v2/answers.py` và `app/v2/planning.py`. Do đó, bộ này không còn là held-out
độc lập cho các thay đổi đó. Các kết quả P8 phía trên vẫn là số liệu lịch sử của
run cũ; source sau remediation **chưa được đánh giá lại**. Cần khóa một challenge
set mới chưa dùng trong phát triển và thẩm định mù trước khi đưa ra kết luận xác
nhận. Artifact không ghi SUT Git SHA, nên source revision đã tạo run cũ vẫn
**NOT VERIFIED**.

#### 5. Ranh Giới Khoa Học & Định Hướng Đánh Giá Cốt Lõi

> [!IMPORTANT]
> **Ý NGHĨA THỰC CỦA CHỈ SỐ 720/720 Ô QUAN SÁT:**
> - **720/720 là độ phủ thực thi (Execution Coverage / Observation Completeness):** Con số này xác nhận toàn bộ ma trận kiểm thử ($60 \text{ cases} \times 4 \text{ biến thể} \times 3 \text{ repeats} = 720$ ô) đã hoàn tất chạy và thu thập dữ liệu đầy đủ vào sổ cái PostgreSQL (`codex-p7-p8-ledger`), không có ô nào bị thiếu, hủy bỏ hoặc lỗi mồ côi.
> - **720/720 KHÔNG PHẢI là độ chính xác hay tỷ lệ thành công tác vụ:** Con số này hoàn toàn không đồng nghĩa với việc 720 lượt đều thành công hay chứng minh Multi-Agent tốt hơn Single-Agent. Trên thực tế, Task Completion của các biến thể chỉ đạt trong khoảng **33% – 36%** (`ma_adaptive_rag`: 35.56%, `sa_shared_tools_rag`: 36.11%).

##### Giả thuyết nghiên cứu hẹp cho phần cốt lõi (Adaptive RAG):
1. **Dữ liệu ủng hộ cải thiện chất lượng căn cứ (Grounding) cục bộ:** So với Fixed RAG, cơ chế Adaptive RAG đem lại cải thiện có ý nghĩa thống kê về **Claim Support** ($\Delta = +4.12\%$, 95% CI: $[+0.51\%, +8.13\%]$), **Citation Precision** ($\Delta = +9.34\%$, 95% CI: $[+3.05\%, +16.80\%]$) và khả năng dẫn giải hữu ích (`useful_continuation` $\Delta = +18.72\%$).
2. **Chưa có bằng chứng tăng Task Completion tổng thể:** Khi đối chiếu với Single-Agent baseline (`sa_shared_tools_rag`), `ma_adaptive_rag` đạt 35.56% so với 36.11% ($\Delta = -0.55\%$, 95% CI: $[-4.12\%, +3.70\%]$, 3 thắng, 20 hòa, 4 thua). Sự khác biệt này không có ý nghĩa thống kê; kiến trúc Multi-Agent chưa thể hiện ưu thế vượt trội về độ hoàn thành nhiệm vụ chung cuộc.
3. **Đánh đổi vận hành & Rủi ro phân quyền:** Adaptive RAG làm tăng độ trễ trung bình ~3.8s – 4.0s và khiến tỷ lệ tuân thủ phân quyền (**Authorization**) giảm xuống **86.67%** (do cơ chế thích ứng tự động kích hoạt công cụ vượt blueprint cho phép trong 20.7% số ca thất bại, trong khi Fixed RAG đạt 100.00%).

##### Nguyên tắc cho các đợt đánh giá tiếp theo:
- **Dừng mở rộng benchmark thăm dò ad-hoc:** Không tiếp tục tạo các kịch bản benchmark thăm dò đơn lẻ.
- **Khóa tiêu chí thẩm định trên runtime chính:** Mọi đợt đo đạc mới chỉ được tiến hành khi đã khóa phương pháp thẩm định trực tiếp trên runtime sản phẩm chuẩn ([`app/v2`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/app/v2)), kết hợp đối chiếu nguồn snapshot (source-grounding) và một phần thẩm định mù bởi con người (blind human audit) thay vì chỉ phụ thuộc vào automated LLM Judge.

### 12.4. P7/P8 r3 sau sửa parser — 2026-10-12 (development regression)

Run này bám đúng source commit `e99f186eb6662a99cd8397da1ea633473a6b3a54`.
P7 hoàn tất `36/36`; P8 hoàn tất đủ `720/720` ô trên 60 ca × 4 nhánh × 3 lần
lặp, không thiếu hay lỗi. Vì bộ held-out đã được xem khi remediation, đây là
**development regression**, không phải tập xác nhận độc lập. Chưa có semantic
judge, đánh giá người, khoảng tin cậy cho correctness, hoặc kết quả operate.

#### Kết quả vận hành thô

`answered` là outcome của ứng dụng ở lượt cuối, không đồng nghĩa câu trả lời đúng
hoặc có nguồn hỗ trợ. Latency E2E mỗi ca là tổng `elapsed_seconds` qua các lượt;
p50 là median, p95 là nearest-rank. Chi phí là known ledger cost, không phải hóa
đơn provider.

| Nhánh | `answered` / 180 | p50 / p95 (giây) | Known cost (USD) | Input + output tokens |
| --- | ---: | ---: | ---: | ---: |
| `sa_shared_tools_rag` | 120 | 11,484 / 25,562 | 1,28674557 | 908.330 |
| `ma_fixed_rag` | 120 | 9,508 / 28,500 | 1,30889818 | 965.040 |
| `ma_adaptive_rag` | 119 | 10,820 / 28,000 | 1,36081138 | 989.113 |
| `ma_adaptive_no_rag` | 0 | 1,649 / 8,047 | 0,32210475 | 246.388 |

Tổng P8 là `$4.27855988`, 3.108.871 input+output tokens, reservation chưa quyết
toán `$0`. Ở lượt cuối, `answer_repair_not_grounded` xuất hiện 51/180 lần ở
Adaptive RAG, 51/180 Fixed RAG, 57/180 Single-Agent RAG; `evidence_incomplete`
xuất hiện 21/180 ở mỗi nhánh RAG và 140/180 ở No-RAG. Đây là cảnh báo pipeline,
không thay thế chấm semantic.

#### Lỗi được sửa và giới hạn kết luận

Trace cho thấy parser entity queries trước đây tách liên từ `Và` viết hoa bên
trong tên sách, làm truy vấn catalog trở nên mơ hồ. Regex trong
`app/v2/planning.py` hiện chỉ tách `và`/`với` viết thường và vẫn tách các liên từ
tiếng Anh không phân biệt hoa thường. Trên các ca `held_multi_expert_03`, `_04`,
`_10`, `_11`, cả Fixed MA và SA + RAG đều `answered` 3/3 lần; Adaptive MA + RAG
đạt 3/3 ở ba ca, 2/3 ở `_10`; No-RAG abstain 3/3 ở cả bốn ca. Đây xác nhận lỗi
phân tích tên sách đã được xử lý trong các trace mục tiêu, không chứng minh chất
lượng ngữ nghĩa hay lợi thế kiến trúc MA.

Trace còn cho thấy giới hạn thiết kế benchmark: cấu hình Adaptive cho phép tối đa
một continuation, nhưng **0/216** lượt người dùng Adaptive có revision kế hoạch
sau lượt thực thi đầu. Vì vậy workload này chưa kiểm tra được giá trị của
continuation; chỉ có thể đánh giá kế hoạch ban đầu. Qua toàn bộ 216 lượt mỗi
nhánh, fallback `answer_repair_missing_required_knowledge` xuất hiện 49 lần ở
Adaptive MA, 47 ở Fixed MA và 54 ở SA. Checkpoint không lưu văn bản draft/repair
trung gian, nên trace hiện tại không phân biệt được model bỏ selector bằng chứng
hay verifier bác một paraphrase. Cần lưu rejection code và candidate/claim ID
đủ để chẩn đoán trong các đợt development tiếp theo; không suy ra các câu trả
lời đó sai chỉ từ warning.

Gói raw đầy đủ, manifest, hash và summary nằm tại
[`evaluation/v3/results/p8-trace-20261012-r3`](../evaluation/v3/results/p8-trace-20261012-r3/README.md).
Gói này không chứa `.env` hoặc credential; cần dựng lại PostgreSQL/vector index
từ source snapshot đã pin để chạy lại live. Bộ reference 32 ca trong gói chỉ là
development calibration. LLM judge/operate chưa chạy; kết quả r3 không được dùng
như xác nhận độc lập.

### 12.5. P7/P8 r7 — 2026-10-13 (development trace, chưa chấm)

Run này gắn với source commit
`4238394ee932dbf79a53aaa1a565c5203f857263`; protocol SHA-256
`342906ef4713edaef23fc0b829780ff7d70a3bc1f64fa58cd36cefe28f999a2a`. P7 có
`36/36`; P8 có `720/720` trên 60 ca × 4 nhánh × 3 repeats, không thiếu/lỗi ô.
Held-out đã được dùng trong remediation, nên đây là **development regression**.
Chưa có semantic judge, người chấm, correctness CI hay operate.

| Nhánh | Calls | Known cost (USD) | p50 / p95 observation (giây) | `answered` / 216 user turns |
| --- | ---: | ---: | ---: | ---: |
| Adaptive MA + RAG | 874 | 1.601708 | 11.172 / 27.006 | 168 |
| Single-Agent + RAG | 773 | 1.432035 | 11.125 / 24.429 | 168 |
| Fixed MA + RAG | 700 | 1.457079 | 9.438 / 24.664 | 168 |
| Adaptive MA không RAG | 234 | 0.339016 | 1.640 / 7.193 | 33 |

Mỗi nhánh có 180 observations và 27 work groups; outcome có 216 user turns do
một số case nhiều lượt. Adaptive MA + RAG dùng 13,1% thêm generation calls,
11,85% thêm known cost và p95 cao hơn 10,55% so với SA + RAG. Với các case có
8 required fact blueprints, fallback `answer_repair_missing_required_knowledge`
xuất hiện ở 32/36 Adaptive, 30/36 Fixed và 33/36 SA observations; mỗi nhánh chỉ
có 12 case trong strata này, lặp ba lần.

Code path cho thấy warning xuất hiện sau khi repaired draft vẫn thiếu yêu cầu;
sau đó answer producer dùng deterministic path trên cùng evidence. Đây là dấu
hiệu đường answer/grounding recovery dùng chung, không chứng minh retrieval miss
hay câu trả lời sai. Checkpoint chỉ lưu retrieval count và final cited evidence,
không lưu toàn bộ retrieval hits; document recall vì vậy chưa đo được. Citation
IDs nối đúng trong JSON không phải kiểm định semantic citation support.

Checkpoint SHA-256
`cd9f7c0de094e1c7935880f3adc3c210604ac24c66f8cd9caede58f804e53261`; schedule
SHA-256 `e07a5e9996f12a57093a66f17de7585f63c0c1f0120cabf17aa2866fea495cef`.
Raw trace audit, script tái tạo và toàn bộ số đếm nằm tại
[`evaluation/v3/results/p8-trace-20261013-r7`](../evaluation/v3/results/p8-trace-20261013-r7/README.md).
Đây không phải tập xác nhận mới; protocol xác nhận vẫn là draft.

### 12.6. LLM reference calibration — 2026-10-13 (development only)

Đã tạo 32 nhãn tham chiếu bằng `gpt-6-luna` với `store=false` cho 32
observation đo của pilot P7 (`pilot-live-v3`: 8 ca development × 4 nhánh); đây
là nhãn tham chiếu để hiệu chuẩn judge P8 trong tương lai, không phải kết quả
chấm câu trả lời P8. Tổng chi phí ước tính `$0.0064505`, dưới cap `$0.25`.
Packet, output đầy đủ, request manifest và đúng bản script đã chạy được lưu tại
[`evaluation/v3/thesis-validation-v1/dev-judge-calibration-20261013`](../evaluation/v3/thesis-validation-v1/dev-judge-calibration-20261013/README.md).

Các nhãn này đã được ghép với receipt P7 bằng hash JSON canonical, nhưng vẫn là
reference do LLM tạo trên dữ liệu development, không phải ground truth của
người chấm. Chúng chưa được dùng để đối chiếu judge
`gpt-5.4-mini-2026-03-17`; 752 lượt P8 judge đã duyệt chưa được chạy, và chúng
không chấm P8 r7 ở mục 12.5. Vì vậy vẫn chưa có kết luận semantic về MA so với
SA.

Trace P7 r5 ghi `ma_adaptive_rag` trả `action_mode_mismatch` ở ca
`dev_shopping_merchant_01`; r6 và r7 ghi nhánh này trả thẻ checkout chờ xác
nhận. Code hiện tại có thay đổi planner tại commit `e2d1f8b` xử lý an toàn
trường hợp model plan không được phép trong shopper mode; chuỗi trace phù hợp
với một fix có ích. Đây chưa phải kiểm định nhân quả: protocol assets đổi giữa
r5/r6/r7, cả ba dùng gold `f6988415…` và split `d4af32e7…`, khác hash hiện tại
`31bcb31c…` và `49adf66b…`, đồng thời checkpoint không bind Git SHA của SUT.
Ở cấp từng case, cả 8 execution-case hash trong mỗi run r5/r6/r7 đều khớp 8
case pilot hiện tại; sai khác gold/split tổng thể nằm ở protocol/roster đầy đủ.
Raw checkpoint, schedule, protocol, script và manifest đã được lưu tại
[`P7 trace archive`](../evaluation/v3/results/p7-trace-20261013/README.md).

Trong 32 lượt đo P7 r7, Adaptive RAG và SA đều có 6 `answered`, 1
`needs_clarification`, 1 `awaiting_confirmation`. Adaptive RAG dùng 40 generation
calls, `$0.077554`, p50 `10.883s`; SA dùng 35 calls, `$0.070811`, p50 `9.406s`.
Đây là outcome/chi phí/latency mô tả trên 8 ca development, không phải điểm đúng
ngữ nghĩa, CI hay xác nhận kiến trúc. Pilot chưa cho thấy lợi ích outcome quan
sát được của MA để bù chi phí tăng.

Ở `dev_multi_expert_01`, cả hai nhánh Adaptive RAG và SA trả bản liệt kê
evidence dài 1.803 ký tự, không có câu so sánh hay khuyến nghị trực tiếp dù
prompt yêu cầu; cả hai có cảnh báo `answer_repair_not_grounded`. Đây là điểm yếu
tổng hợp câu trả lời dùng chung: verifier hiện từ chối claim có phép so sánh.
Nó là ứng viên sửa trên development, nhưng phải được áp dụng công bằng cho các
arm và kiểm tra lại bằng P7/P8 trước khi dùng làm kết quả luận văn.

