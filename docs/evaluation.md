# Phương pháp evaluation

## 1. Câu hỏi nghiên cứu và giới hạn

Evaluation hiện tại kiểm tra:

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
| v3 Package 7/8 | Frozen corpus/evaluation split, pilot và held-out benchmark | Historical corrected P7/P8 preserved; source remediation requires an unstarted successor P7, then successor P8 |

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

## 8. Package 7 historical protocol và successor pilot

**Cập nhật sau P02:** xem [handoff hiện hành](thanh-v2/p02-successor-handoff.md).
Protocol `c8a4…` và dry-run bên dưới là trạng thái bàn giao `31f3351` trước sửa
P02. Mã hiện hành có protocol `315efff0d596f11ea63aa60729834e54fb5d6acb9bf6b7d2b9260b96b601451f`;
dry-run vẫn đủ 36 cells. Operator đã hoãn PostgreSQL và live P7/P8; chưa freeze,
chưa tạo P8 mới và chưa chạy `operate`.

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

P7 successor chưa dispatch. Dry-run đã xác nhận `36` cells gồm `4` warmup và
`32` measurements cho `run_p7_successor_v4`, với schedule SHA
`754b168a7d361986c98c243b3fbd7e69fbdbf3c7dd4177e0d90f5f49492f923e`. P8
successor chỉ được tạo sau khi pilot này hoàn tất và repeat decision mới được
freeze; không resume hay ghép thêm cells vào P7/P8 historical.

Remediation P02 hiện hành đọc/ground giá nhiều offer rồi tạo proposal nếu có
target độc lập do server xác định. Thiếu target vẫn trả clarification an toàn.
P7/P8 phải ghi nhận kết quả thực tế theo gold contract; không suy diễn proposal
hay fabricate citation để đạt một trạng thái gold dự kiến.

Semantic scoring được khai báo là automated model judge (`model_judge`) theo
judge configuration/schema đã hash; không có human semantic judge. Deterministic
metrics (citation precision/coverage và document recall) vẫn phải lấy từ
receipt/evidence contracts. Calibration phải được freeze trước held-out scoring.

## 9. Package 8 additive held-out driver

Package 8 chỉ bổ sung các module `benchmark_*.py`. Mỗi P8 run bind chính xác
protocol và repeat decision của P7 tương ứng; historical P7/P8 artifacts chỉ
được đọc như evidence. CLI có các lệnh local `validate`, `dry-run`, `prepare`,
hai lệnh SUT `run`/`resume`, và `operate` cho lifecycle sau khi SUT đã hoàn tất:

```powershell
python -m app.evaluation.benchmark_cli validate
python -m app.evaluation.benchmark_cli dry-run --run-id run_p8_dry
python -m app.evaluation.benchmark_cli prepare `
  --output output/evaluation-v3/heldout
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

Held-out SUT `run_p8_heldout_corrected_v3` đã terminal partial: `678` completed,
`42` failed trên `720` scheduled, không có pending, ambiguous hoặc orphan cell.
Schedule SHA là
`a6002ab2dc15282bf4b26c79ffece061242065e8352e637a1a82623a8fa1fd71`. Record
partial account `792` generation/provider attempts, `0` retries, `0` embeddings,
`1.44856770 USD` known SUT cost và không có unresolved reservation. Các safe
failure code được ghi nhận là 1 `expert_selection_not_authorized`, 2
`model_response_incomplete`, 4 `model_response_invalid` và 35
`turn_execution_failed`; raw provider payload hay fabricated citation không được
đưa vào report. Partial report checksum là
`17177bd7829d972c159d77ca068852fafd10d12152f2d087db07172f768f62ea`, execution
case-set SHA là
`26cb17e6dbc549f871b69ef682b21af9f32fa69a05d8c671e297b269c0040339`.
Checkpoint append-only không được resume để redispatch các terminal cell. Run
này không được trình bày như scored, calibrated, judge-complete hay benchmark
quality. Chưa có calibration/judge journal, final result hoặc paired metric từ
run này; P8 vẫn chờ P7 successor hoàn tất, freeze repeat decision và tạo một P8
successor riêng theo source protocol mới, trước khi exact immutable evidence
resolver, calibration/judging và các final gates có thể chạy.

Để đóng Package 8, phải có đủ các gate sau:

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

## 11. Điều kiện trước claim mạnh hơn

1. Freeze một complete v2 bundle từ clean revision và validate độc lập.
2. Dùng human/semantic rubric do curator độc lập thiết kế; báo agreement.
3. Replicate qua nhiều seed/model snapshots; không cherry-pick.
4. Báo paired effect/interval/win-tie-loss cùng absolute quality, latency, token
   và estimated/billed cost tách biệt.
5. Thêm load/soak, chaos, drift, fairness, PII/security evaluation trước mọi
   production hoặc marketplace claim.
