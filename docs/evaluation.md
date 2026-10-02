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
| v3 Package 7/8 | Frozen corpus/evaluation split, pilot và held-out benchmark | P7 v10 is frozen on protocol `36ac8f…` with 3 repeats. P8 v16 has 720/720 SUT cells; its development calibration failed the frozen threshold, so no blind score or final metrics exist. See §8 and §9. |

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
Protocol `c8a4…` và v4 artifacts bên dưới là lịch sử trước sửa P02. P7 v5
(`28621f…`) và v8 (`a249d0…`) cũng là lịch sử. Chẩn đoán `operate` v11 cho
thấy JSON schema cho phép cả chín evaluation metric trong khi semantic validator
chỉ nhận sáu. Commit `d030f9a` giới hạn metric enum; `v3_cli validate` hiện xác nhận
protocol `36ac8fec094b201345d74a324aa569057cb1dae5c506cd78763ee327ad5f983c`.
Current P7 successor `run_p7_successor_v10` completed 36/36 cells with zero
failures and froze 3 repeats. Its repeat-decision SHA-256 is
`9b4a7552919e687bdfe57a67f144571d26daf1c0ffbbcd432dc721411386c3df`; projected
held-out SUT cost is `$5.97439800`. P7 v9's 13/36 partial run and the old
`aaa7026…`/`0b218d30…` preflights are historical. The user has approved the
OpenAI Embeddings API for P7/P8.

P8 v14 had 720/720 SUT cells but its `operate` stopped at exact `trust` evidence
resolution. P8 v15 was an interrupted network partial and is retained only for
audit. Official successor `run_p8_successor_v16` completed 720/720 SUT cells
with zero failed, missing, ambiguous, pending or orphan cells under schedule
SHA `5c7930f734fb29ede24ef04c3cfec64c7c3fd70bc241a76f8ed93918a63ddb25`.
`operate` passed exact evidence resolution and generated all 32 development
calibration records, but the frozen max-absolute-error threshold `0.25` failed
for all six semantic metrics (maximum observed error `1.0` for each). No
calibration freeze, blind held-out judgment, final score, paired metrics or
publication exists for v16.

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
judge configuration/schema đã hash; không có human semantic judge. Deterministic
metrics (citation precision/coverage và document recall) vẫn phải lấy từ
receipt/evidence contracts. Calibration phải được freeze trước held-out scoring.

## 9. Package 8 additive held-out driver

Package 8 chỉ bổ sung các module `benchmark_*.py`. Mỗi P8 run bind chính xác
protocol và repeat decision của P7 tương ứng; historical P7/P8 artifacts chỉ
được đọc như evidence. CLI có các lệnh local `validate`, `dry-run`, `prepare`,
hai lệnh SUT `run`/`resume`, và `operate` cho lifecycle sau khi SUT đã hoàn tất:

```powershell
$p7Output = 'output/evaluation-v3/pilot-successor-v10'
$p8Output = 'output/evaluation-v3/heldout-successor-v16'
$p7RuntimeManifest = 'output/evaluation-v3/current-machine-ledger/runtime-manifest-v6.json'
python -m app.evaluation.benchmark_cli validate `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json"
python -m app.evaluation.benchmark_cli dry-run `
  --run-id run_p8_successor_v16 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json"
python -m app.evaluation.benchmark_cli prepare `
  --run-id run_p8_successor_v16 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json" `
  --pilot-checkpoint "$p7Output/pilot-checkpoint.v3.jsonl" `
  --pilot-schedule "$p7Output/pilot-schedule.v3.json" `
  --output $p8Output
python -m app.evaluation.benchmark_cli run `
  --run-id run_p8_successor_v16 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json" `
  --allow-network `
  --database-url $env:DATABASE_URL `
  --output $p8Output
python -m app.evaluation.benchmark_cli resume `
  --run-id run_p8_successor_v16 `
  --p7-runtime-manifest $p7RuntimeManifest `
  --p7-protocol "$p7Output/protocol.v3.json" `
  --p7-repeat-decision "$p7Output/repeat-decision.v3.json" `
  --allow-network `
  --database-url $env:DATABASE_URL `
  --output $p8Output
python -m app.evaluation.benchmark_cli operate `
  --run-id run_p8_successor_v16 `
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
user approved it on 2026-10-01. Current successors are P7 v10 and P8 v16, as
recorded above.

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
settlement record is unavailable for direct inspection. Keep the A amount
separate from the cumulative live-ledger total until reconciliation. After explicit user
approval to send the held-out prompts and fixtures to the model provider, the
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
historical. P7 v10 is frozen. P8 v16 has full SUT coverage and exact evidence
resolution, but its development calibration failed; do not report a benchmark
score or paired metrics. No benchmark conclusion is drawn from the diagnostic
runs.

The checkout `.env` still points to SQLite; successor runs use the isolated
PostgreSQL ledger. Its 2026-10-02 02:07 UTC snapshot is `$19.60072061` known,
`$0.32514984` unknown, `$0` reserved/active/pending, and `$80.07412955` remaining
under the `$100` cap. The unknown balance includes the historical carry-forward
`$0.18567534`, P8 v15's `$0.13102500`, and one P8 v16 judge timeout `$0.00844950`.
Diagnostic A's `$0.05446350` is identified as an aggregate from historical clone
`p8_failed_diag_20260923_a`; original attempt IDs are unavailable, so it remains
unknown carry-forward. P7 v10's `$0.05652510` matches 34 database attempts and
its checkpoint; P8 v16 SUT cost `$1.47381435` matches 768 known attempts.
Across the initial and retry journals, judge accounting shows 33 known attempts
(`$0.08778375`) and one unknown timeout (`$0.00844950`); the rejected job was
retried under a fresh scope. The reconciliation is
`output/evaluation-v3/current-machine-ledger/ledger-reconciliation-20261002.json`;
the earlier 2026-10-01 snapshot remains historical. Credentials are local-only
and excluded from Git.

P7 v9 is a historical partial at `13/36`; P7 v10 has completed and frozen the
repeat decision. P8 v14 completed SUT coverage but stopped at the then-unsupported
`trust` evidence kind; v15 stopped after sandbox network failures. P8 v16 has
full SUT coverage, but judge calibration is the current failing gate.

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
