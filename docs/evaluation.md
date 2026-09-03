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

## 2. Frozen inputs và provenance

- Gold cases: [`evaluation/cases.v1.json`](../evaluation/cases.v1.json)
- Robustness corpus: [`evaluation/corpus.v2.json`](../evaluation/corpus.v2.json)
- Paired experiment: [`evaluation/experiment.v2.json`](../evaluation/experiment.v2.json)
- Bounded live configuration: [`evaluation/experiment.live-pilot.v2.json`](../evaluation/experiment.live-pilot.v2.json)
- Pricing manifest: [`evaluation/pricing/openai-standard-2026-08-25.v2.json`](../evaluation/pricing/openai-standard-2026-08-25.v2.json)

Current dataset là `tiki_books_vi_28_v1`: 28 clean cases và 16
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

## 3. Variant hierarchy

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

## 4. Paired protocol

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

## 8. Evidence đã commit

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

## 9. Điều kiện trước claim mạnh hơn

1. Freeze một complete v2 bundle từ clean revision và validate độc lập.
2. Dùng human/semantic rubric do curator độc lập thiết kế; báo agreement.
3. Replicate qua nhiều seed/model snapshots; không cherry-pick.
4. Báo paired effect/interval/win-tie-loss cùng absolute quality, latency, token
   và estimated/billed cost tách biệt.
5. Thêm load/soak, chaos, drift, fairness, PII/security evaluation trước mọi
   production hoặc marketplace claim.
