# Phương pháp Evaluation

## 1. Mục tiêu

Evaluation trả lời ba câu hỏi có thể kiểm chứng:

1. Router/planner có chọn đúng intent và tập action đã gắn nhãn không?
2. Hệ thống có hoàn thành task, chỉ dùng candidate/facts phù hợp và giữ
   provenance khi có dữ liệu/agent lỗi không?
3. Kết quả có tái lập trên snapshot mẫu hiện tại không?

Nó **không** chứng minh chất lượng trên dữ liệu marketplace thật hoặc semantic
correctness tự do. Repository có cả frozen single-agent real-model baseline và
real-model Multi-Agent capture; bảng chênh lệch hiện là descriptive vì prompt,
runtime orchestration và số repetition chưa đồng nhất.

## 2. Frozen artifacts

- Corpus: [`evaluation/cases.v1.json`](../evaluation/cases.v1.json)
- Baseline manifest:
  [`evaluation/baselines/single_agent.v1.json`](../evaluation/baselines/single_agent.v1.json)
- Single-agent real-model observations/report:
  [`evaluation/results/baseline-single-agent-v1`](../evaluation/results/baseline-single-agent-v1)
- Multi-Agent real-model observations/report:
  [`evaluation/results/real-multi-agent-v1`](../evaluation/results/real-multi-agent-v1)
- Reference report:
  [`evaluation/results/reference-v1/report.md`](../evaluation/results/reference-v1/report.md)

Mỗi report ghi SHA-256 của corpus, `app/db/seed.py` và manifest có thứ tự của mọi
tệp Python trong `app/`, cùng Python/platform, app version, seed `42`, row counts
và số lần lặp. Manifest hash từng nội dung tệp kèm relative path, nên thay đổi
source SUT sẽ tạo identifier mới. Gold labels được curator đóng băng trong JSON;
runner không truy vấn SUT để tự sinh expected values.

## 3. Corpus design

Đúng 28 case, bốn case mỗi nhóm yêu cầu trong workflow:

| Category | Mục tiêu |
| --- | --- |
| `simple` | Exact search và review theo product ID |
| `complex` | Comparison nhiều bước, filtered ranking, market aggregate |
| `multi_domain` | Product + Review + Trust top-N recommendation |
| `missing_data` | Unknown product, mixed comparison, impossible filter |
| `tool_failure` | Inject Product/Review/Trust agent failure có kiểm soát |
| `ambiguous` | Regression policy cho yêu cầu thiếu context |
| `irrelevant` | Không gọi tool ngoài bốn domain hỗ trợ |

Mỗi case khai báo accepted intents/statuses, action multiset (giữ duplicate),
relevant product IDs hoặc explicit `null`, structured answer assertions và, nếu
có, exact failure injection.

## 4. Execution protocol

- Tạo SQLite tạm mới và seed đúng 5 shop/30 sản phẩm/150 review cho mỗi run.
- Tạo Orchestrator/session mới cho từng case; không rò state giữa case.
- IDs được derive ổn định từ case/repetition.
- Không import/call OpenAI integration runner, không gọi network hay Qdrant.
- Static market notes là local deterministic adapter của production interface.
- Repetition `0` dùng cho correctness; mọi repetition dùng cho latency.
- Failure dispatcher chỉ fail đúng `(agent_id, action)` đã gắn nhãn và trả
  `AgentError` an toàn.

Library runner tạm bind DB session factory process-wide, vì vậy CLI được thiết
kế chạy trong process evaluation cô lập; không nhúng concurrent benchmark vào
process web production.

Real-model Multi-Agent capture dùng script riêng và chỉ chạy khi được gọi rõ
ràng:

```powershell
python scripts/run_real_multi_agent_benchmark.py --repeats 1
```

Script vẫn seed SQLite cô lập và chạy router/planner/domain agents deterministic
như protocol trên, sau đó gọi OpenAI Responses API một lần cho lớp tổng hợp cuối
mỗi case. Input gửi cho model gồm evidence có provenance và deterministic draft;
model được phép chỉnh trình bày nhưng không được xoá facts, warning, refusal hay
partial-success caveat. `--score-only` chỉ đọc artifact đã capture, không cần
network hoặc `OPENAI_API_KEY`, nên mới được dùng trong CI.

## 5. Metric definitions

### Routing Accuracy

```text
mean(predicted_intent ∈ accepted_intents)
```

### Tool Selection

Action là multiset để comparison có thể yêu cầu `product.search` hai lần.

```text
TP = Σ_action min(predicted_count, expected_count)
precision = TP / predicted_count
recall = TP / expected_count
F1 = harmonic_mean(precision, recall)
```

No-tool cases được báo riêng bằng `no_tool_correctness`; zero denominator là
`N/A` với reason, không ép thành 0.

### Task Success Rate

Một case pass khi terminal status thuộc accepted statuses và mọi assertion
`critical` pass. Partial success có thể là kết quả đúng chỉ với case được gắn
nhãn như vậy.

### Answer Assertion Accuracy

Tỷ lệ structured assertions pass: normalized `contains`/`excludes`, exact
selected product ID, minimum provenance và exact safe error code. Đây là rubric
deterministic, không phải đánh giá toàn bộ ý nghĩa văn bản.

### Retrieval

So sánh set predicted IDs với frozen relevant IDs. Case `relevant_product_ids:
null` không tham gia retrieval metric. Gold/predicted đều rỗng được báo qua
`empty_retrieval_correctness`, không dùng để làm tăng precision/recall.

### Failure

- `agent_failure_rate`: failed agent steps / attempted agent steps; gồm cả
  expected missing-dependency và injected failures.
- `partial_recovery_rate`: recoverable injected cases vẫn đạt task success /
  recoverable injected cases.
- `provenance_case_coverage`: non-failed agent-backed cases có ít nhất một
  provenance record.

### Latency, token và cost

- p50/p95 dùng nearest-rank trên adapter-level local elapsed time;
- real-model p50/p95 dùng toàn bộ elapsed time (orchestration + API), đồng thời
  report thêm `model_latency_p50_ms`/`model_latency_p95_ms` cho riêng API;
- deterministic v1 runtime quan sát 0 model calls, nhưng token/cost production
  được ghi **N/A**, không ghi 0.
- Real artifact ghi token usage từ Responses API; pricing/provider billing vẫn
  `N/A` nếu không có dữ liệu chi phí đáng tin cậy.

## 6. Reference result v1

Snapshot hiện tại chạy 28 case × 3 lần lặp, 84 observations:

Reference artifact dùng report schema `1.1` và công khai SUT source-manifest
SHA-256 ngay đầu `report.md`; `report.json` còn lưu danh sách 88 tệp đã hash để
có thể tái tạo phép kiểm tra.

| Metric | Reference result |
| --- | ---: |
| Routing accuracy | 28/28 |
| Tool selection precision/recall | 46/46 |
| Exact plan | 28/28 |
| Task success | 28/28 |
| Answer assertions | 86/86 |
| Retrieval precision/recall | 31/31 |
| Empty retrieval correctness | 9/9 |
| Recoverable injected failure | 2/2 |
| Provenance case coverage | 23/23 applicable |

Latency là số phụ thuộc máy và xem trực tiếp trong report snapshot. Agent
failure rate reference là 8/46 vì corpus chủ động chứa missing-data và injected
failure; không diễn giải nó như production incident rate.

Kết quả 100% phù hợp cho **regression corpus đồng phát triển với deterministic
system**. Nó không phải external validity, không có confidence đủ cho thị
trường thật và không được dùng làm claim “AI chính xác 100%”.

## 7. Real Multi-Agent result v1

Capture tại revision `6124a0e` chạy 28 case × 1 repetition bằng
`gpt-5.4-mini`, cùng dataset hash với baseline main:

| Metric | Multi-Agent real | Single-Agent main real | Delta (multi − single) |
| --- | ---: | ---: | ---: |
| Answer assertions | 100.00% (86/86) | 12.79% (11/86) | +87.21 pp |
| Tool precision | 100.00% (46/46) | 47.62% (20/42) | +52.38 pp |
| Tool recall | 100.00% (46/46) | 43.48% (20/46) | +56.52 pp |
| Exact plan | 100.00% (28/28) | 50.00% (14/28) | +50.00 pp |
| Retrieval precision | 100.00% (31/31) | 79.41% (27/34) | +20.59 pp |
| Retrieval recall | 100.00% (31/31) | 87.10% (27/31) | +12.90 pp |
| Provenance coverage | 100.00% (23/23) | 0.00% (0/24) | +100.00 pp |
| Task success (frozen rubric) | 100.00% (28/28) | 0.00% (0/28) | +100.00 pp |
| Latency p50 | 1,168 ms | 3,405 ms | −2,238 ms |
| Latency p95 | 2,042 ms | 8,475 ms | −6,433 ms |
| Token usage | 61,778 | 58,878 | +2,900 |

Các delta chỉ mang tính mô tả: Multi-Agent có planner/domain evidence
deterministic và model dùng draft được khóa facts, còn baseline main để model tự
chọn tools/answer; hai prompt/runtime không phải paired treatment. Kết quả này
cho thấy pipeline Multi-Agent hiện giữ plan, provenance và answer assertions tốt
hơn trong frozen rubric, không chứng minh chất lượng tổng quát hay superiority
thống kê. Cần human semantic rubric, nhiều repetition và cùng protocol trước khi
viết claim paired win/tie/loss.

## 8. Baseline honesty

Reference offline vẫn có hai fake-provider traces phục vụ tool-loop regression
và không được dùng thay cho baseline. Baseline thật hiện đã có 28 captured
observations, model/hash/artifact metadata và token/latency; các trường còn thiếu
cho một paired claim gồm:

- cùng provider/runtime cho cả hai hệ thống;
- human/semantic rubric ngoài structured assertions;
- provider pricing/billing để tính cost;
- nhiều repetitions/seed để báo paired win-tie-loss và khoảng tin cậy.

Manifest ghi `real_model_captured`; report baseline giữ nguyên raw structured
tool calls/results và **không** tự suy diễn routing intent ẩn từ tool call.
`ecommerce-evaluate` vẫn là benchmark deterministic regression; real script là
capture bổ sung và không được chạy tự động trong CI.

## 9. Re-run

```powershell
ecommerce-evaluate --repeats 3 --output evaluation/results/latest
```

Smoke một phần corpus:

```powershell
ecommerce-evaluate --repeats 1 --max-cases 4 `
  --output evaluation/results/smoke
```

Chấm lại artifact single-agent đã capture mà không gọi network:

```powershell
python scripts/score_real_baseline.py
```

Chấm lại artifact Multi-Agent real đã capture mà không gọi network:

```powershell
python scripts/run_real_multi_agent_benchmark.py --score-only
```

Outputs:

- `report.json`: metadata, global/category metrics, scores và observations;
- `observations.csv`: normalized per-run execution facts;
- `report.md`: bảng/giới hạn đọc được cho khóa luận.

Baseline raw observations nằm trong `evaluation/results/baseline-single-agent-v1/`;
scorer kiểm tra đủ 28 case, gắn artifact SHA-256 và tách rõ API turn success
khỏi task success theo frozen assertions. Real Multi-Agent raw observations nằm
tại `evaluation/results/real-multi-agent-v1/`; scorer kiểm tra đủ từng
`case_id/repetition`, ghi usage/latency và render bảng so sánh descriptive.

Không overwrite `reference-v1` nếu chưa review diff, hash, all case scores và
runtime environment. Report mới với dataset/seed hash khác là một benchmark
version mới, không phải so sánh trực tiếp mặc định.

## 10. Mở rộng với dữ liệu thật

1. Freeze seed/data manifest và provenance policy mới.
2. Curator độc lập gắn gold facts/relevance; không dùng SUT sinh gold.
3. Thêm semantic/human evaluation rubric và inter-annotator agreement.
4. Chạy lại single-agent và multi-agent bằng cùng provider/model/runtime.
5. Chạy nhiều seed/model runs; tách correctness và latency repetitions.
6. Báo Wilson interval/paired win-tie-loss; không claim significance với sample
   nhỏ.
7. Bổ sung load, chaos, drift, fairness và PII/security evaluation.
