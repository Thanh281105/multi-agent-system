# Phương pháp Evaluation

## 1. Mục tiêu và giới hạn claim

Evaluation v2 trả lời bốn câu hỏi có thể kiểm chứng:

1. Hybrid multi-agent có giữ đúng intent, plan, task assertions và retrieval khi
   so paired với deterministic baseline trên cùng case/repetition không?
2. Mỗi lớp model-assisted routing, planning, specialist và synthesis đóng góp gì
   khi bị loại riêng bằng ablation?
3. Chất lượng thay đổi thế nào trước typo, paraphrase, distractor và prompt
   injection giữ nguyên nhãn?
4. Đổi lại bao nhiêu latency, token và chi phí ước tính theo pricing đã pin?

Protocol không chứng minh chất lượng trên marketplace thật, không thay human
semantic evaluation và không cho phép claim superiority chỉ từ một live smoke.
Structured rubric đo contract đã freeze; nó không chấm toàn bộ sắc thái ngôn ngữ
tự do hoặc mức hữu ích cảm nhận bởi người dùng.

## 2. Frozen inputs v2

- Clean gold cases: [`evaluation/cases.v1.json`](../evaluation/cases.v1.json)
- Robustness corpus: [`evaluation/corpus.v2.json`](../evaluation/corpus.v2.json)
- Full paired experiment: [`evaluation/experiment.v2.json`](../evaluation/experiment.v2.json)
- Bounded live pilot: [`evaluation/experiment.live-pilot.v2.json`](../evaluation/experiment.live-pilot.v2.json)
- Pinned pricing: [`evaluation/pricing/openai-standard-2026-08-25.v2.json`](../evaluation/pricing/openai-standard-2026-08-25.v2.json)

Corpus gồm 28 clean cases v1 và 16 label-preserving transformations: bốn biến
thể cho mỗi parent thuộc simple, complex, multi-domain và irrelevant. Gold của
transformed case kế thừa từ parent; file lưu rõ `parent_case_id`, `transform_id`,
policy và rationale curator. Bảy nhóm clean vẫn là simple, complex,
multi-domain, missing data, tool failure, ambiguous và irrelevant.

Protocol runtime ghi hash SHA-256 của experiment, corpus, base dataset, sample
seed, evaluator và pricing; đồng thời ghi Git revision/dirty state, network
policy, case order, model binding và execution budget. Vì vậy hai observation
chỉ được paired khi cùng protocol hash, không chỉ khi trùng tên case.

## 3. Variants và giả thuyết ablation

| Variant | Model stages | Mục đích |
| --- | --- | --- |
| `deterministic_v2` | Không có | Quality/latency/token/cost floor |
| `hybrid_full` | Routing + planning + specialist + synthesis | Treatment đầy đủ |
| `hybrid_no_router` | Bỏ model routing | Cô lập semantic intent routing |
| `hybrid_no_planner` | Bỏ model planning | Cô lập capability planning |
| `hybrid_no_specialist` | Bỏ agent-local model insight | Cô lập specialist reasoning |
| `hybrid_no_synthesis` | Bỏ model synthesis | Cô lập grounded natural-language synthesis |

Full experiment pin `gpt-5.4-nano-2026-03-17` cho routing, planning và specialist;
`gpt-5.4-mini-2026-03-17` cho synthesis, reasoning effort `low`. Mỗi variant
khai báo chính xác stage/model, parent ablation, max model calls và fallback
policy. Full hybrid tối đa 7 model calls/turn: router, planner, tối đa bốn
specialist calls và một synthesis call.

Experiment và protocol cùng freeze runtime policy không chứa credential:
provider, request timeout, retry, output-token ceiling, concurrency và circuit
breaker. API key không được ghi, hash hay fingerprint vào artifact. Protocol là
nguồn cấu hình runtime thật, không chỉ là metadata mô tả.

Model không sở hữu fact, final status hay tool permission:

- router chọn intent nhưng entity chỉ được dùng khi khớp giá trị Python đã trích
  xuất từ request/session;
- planner chỉ đề xuất capability; Python so với capability policy rồi biên dịch
  DAG đã kiểm tra dependency/step limit;
- specialist chỉ chọn opaque fact ID trong catalog bounded do server tạo;
- synthesis chỉ sắp xếp toàn bộ claim ID trong catalog deterministic;
- Python materialize fact text, claim text, citation, status, selected product,
  warning và sample-data caveat. Model không có trường prose/citation để bịa fact.

## 4. Paired execution protocol

Full configuration freeze:

- correctness: 44 cases × 3 repetitions × 6 variants = 792 observations;
- latency: 7 representative cases × 5 repetitions × 6 variants = 210
  observations;
- warmup: 1 turn/variant, không đưa vào observation matrix;
- tổng measured matrix: 1.002 observations;
- random seed `42`, 10.000 clustered bootstrap samples.

Runner tạo schedule xác định trước. Trong từng case/repetition, thứ tự sáu
variant được shuffle rồi interleave bằng seeded PRNG; correctness và latency dùng
seed stream riêng. So sánh gộp repetitions thành case mean rồi bootstrap theo
**independent base cases**, không giả vờ mỗi repetition là một sample độc lập.

Mỗi turn dùng SQLite tạm đã seed đúng sample dataset và retrieval
`hashed_token_cosine_v1` thực thi thật (không chỉ là nhãn), cùng
Orchestrator/session cô lập. Failure injection chỉ tác động đúng agent/action đã
gắn nhãn. Network mặc định bị chặn; experiment có hybrid variant chỉ chạy khi
người vận hành truyền `--allow-network`. Dirty worktree bị từ chối trừ khi truyền
`--allow-dirty`, và artifact vẫn ghi `git_dirty=true` để không che provenance.

Bundle được ghi vào staging directory rồi atomic rename. Existing output không
bị overwrite. `manifest.json` khóa size/hash/count của `protocol.json`,
`observations.jsonl`, `comparisons.json`, `omissions.json`, `robustness.json`,
`pricing.json` và `report.json`. Validator đọc lại toàn bộ schema/hash, kiểm tra
observation matrix đầy đủ/liên tục, recompute pricing, comparison và robustness;
file thiếu, thừa, symlink hoặc bị sửa đều làm validation fail. Low-level bundle
mặc định là `partial`. Bundle `complete` phải có đúng mỗi analysis cell được
protocol yêu cầu, biểu diễn bởi một comparison hoặc omission hợp lệ, đúng
bootstrap sample/seed; robustness summary cũng phải phủ mọi variant khi corpus
có transformed correctness case.

## 5. Metrics và thống kê

| Metric | Direction | Ý nghĩa |
| --- | --- | --- |
| `task_success` | Cao hơn tốt hơn | Status hợp lệ và mọi critical assertion pass |
| `routing_correct` | Cao hơn tốt hơn | Intent thuộc accepted intents |
| `exact_plan` | Cao hơn tốt hơn | Action multiset đúng, gồm duplicate khi cần |
| `answer_assertion_accuracy` | Cao hơn tốt hơn | Tỷ lệ structured assertions pass |
| `retrieval_f1` | Cao hơn tốt hơn | F1 trên frozen relevant product IDs |
| `end_to_end_latency_ms` | Thấp hơn tốt hơn | Toàn bộ orchestration turn |
| `total_tokens` | Thấp hơn tốt hơn | Provider usage của mọi model stage |
| `estimated_cost_usd` | Thấp hơn tốt hơn | Usage × pricing manifest đã hash |

Mọi delta là `candidate - baseline`; direction quyết định win/loss. Report ghi
baseline/candidate mean, mean/median paired delta, case-level win/tie/loss, 95%
clustered bootstrap interval, paired effect size và exact two-sided sign-test cho
binary metrics khi tính được. Pair có `N/A` ở một phía được đếm vào
`excluded_pair_count`; nếu không còn pair hợp lệ, runner phải ghi omission
`no_comparable_paired_values`, không được biến thành 0 hay âm thầm bỏ metric.

Robustness report ghép từng transformed case với clean parent cùng repetition,
báo task-success rate, intent consistency, mean/maximum degradation và worst
transform cho từng variant. Đây là invariance test trong corpus, không phải
security certification cho mọi prompt injection.

Chi phí là **ước tính** theo manifest có effective date, không phải hóa đơn
provider. Snapshot model trả về từ API, token breakdown, attempts, duration và
fallback reason được capture; prompt, raw provider response và key không được
ghi vào artifact.

## 6. Live pilot đã xác minh

Ngày 2026-08-26 đã chạy bounded pilot sau bằng provider key hiện hành:

```powershell
python -m app.evaluation.v2_runner run `
  --experiment evaluation/experiment.live-pilot.v2.json `
  --variant hybrid_full `
  --max-cases 1 `
  --allow-network `
  --run-id run_live_pilot_20260826_v2

python -m app.evaluation.v2_runner validate `
  --bundle output/evaluation-v2/run_live_pilot_20260826_v2
```

Kết quả đã validate:

| Quan sát | Giá trị |
| --- | ---: |
| Observation / comparison / omission | 2 / 7 / 0 |
| Hybrid model stages thành công | 4/4 |
| Model snapshots | `gpt-5.4-nano-2026-03-17`, `gpt-5.4-mini-2026-03-17` |
| Total tokens | 2.645 |
| Estimated cost | USD 0.00236865 |
| Hybrid end-to-end latency | 16.326,4788 ms |
| Task/routing/plan/assertion/retrieval | 1.0 ở cả hai variant |
| Protocol SHA-256 | `01c6597ef20a94e24739803e06186e0bfb073da1157a520e6ba957c154cf9dbe` |

Một case chỉ chứng minh wiring thật, structured stage execution, usage/cost
capture và artifact integrity. Năm quality metrics hòa `1–1`; token/cost cao hơn
deterministic là expected. Không suy diễn confidence interval một-case thành
độ ổn định hoặc superiority. Full 1.002-observation live experiment chưa được
chạy và không chạy trong CI vì tốn network, tiền và thời gian.

## 7. Chạy và kiểm tra v2

Chạy full experiment từ clean revision:

```powershell
python -m app.evaluation.v2_runner run `
  --experiment evaluation/experiment.v2.json `
  --allow-network `
  --run-id run_thesis_v2
```

Smoke ít case nhưng vẫn tự thêm paired baseline khi chọn candidate:

```powershell
python -m app.evaluation.v2_runner run `
  --experiment evaluation/experiment.live-pilot.v2.json `
  --variant hybrid_full `
  --max-cases 1 `
  --allow-network `
  --run-id run_smoke_v2
```

Validate bundle và recompute một comparison:

```powershell
python -m app.evaluation.v2_runner validate `
  --bundle output/evaluation-v2/run_thesis_v2

python -m app.evaluation.v2_runner compare `
  --bundle output/evaluation-v2/run_thesis_v2 `
  --baseline deterministic_v2 `
  --candidate hybrid_full `
  --metric task_success `
  --phase correctness
```

Không commit bundle local theo mặc định. Chỉ freeze một report khóa luận sau khi
đã review protocol hash, revision sạch, đủ matrix, omissions, model snapshots,
pricing date và mọi artifact hash.

## 8. Artifact v1 lịch sử

V1 được giữ để regression và tái kiểm tra lịch sử, không phải paired evidence
cho runtime mới:

- deterministic 28-case × 3 reference:
  [`evaluation/results/reference-v1`](../evaluation/results/reference-v1);
- frozen single-agent real capture:
  [`evaluation/results/baseline-single-agent-v1`](../evaluation/results/baseline-single-agent-v1);
- deterministic-agents + one-call synthesis capture:
  [`evaluation/results/real-multi-agent-v1`](../evaluation/results/real-multi-agent-v1).

Hai real artifact v1 dùng prompt/runtime khác nhau và một repetition nên delta
chỉ descriptive. Con số 100% trong regression rubric không phải “AI chính xác
100%” và không phải external validity. V1 ghi trạng thái `real_model_captured`
cùng `source-manifest` SHA-256 để ràng buộc artifact với source SUT đã chạy. Các
runner tương thích vẫn có thể dùng để reproduce/chấm lại:

```powershell
ecommerce-evaluate --repeats 3 --output evaluation/results/latest
python scripts/score_real_baseline.py
python scripts/run_real_multi_agent_benchmark.py --score-only
```

## 9. Điều kiện trước claim khóa luận mạnh hơn

1. Chạy đủ protocol v2 từ clean revision và lưu immutable bundle đã validate.
2. Dùng curator độc lập/human semantic rubric, báo rubric và inter-annotator
   agreement; không dùng SUT tạo gold.
3. Replicate qua nhiều seed/model snapshot và báo sensitivity, không cherry-pick.
4. Báo paired effect/interval/win-tie-loss cùng absolute quality, latency, token
   và estimated/billed cost tách biệt.
5. Thêm load/soak, chaos, drift, fairness, PII/security evaluation trước claim
   production trên dữ liệu/người dùng thật.
