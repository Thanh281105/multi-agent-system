# Phương pháp Evaluation

## 1. Mục tiêu

Evaluation trả lời ba câu hỏi có thể kiểm chứng:

1. Router/planner có chọn đúng intent và tập action đã gắn nhãn không?
2. Hệ thống có hoàn thành task, chỉ dùng candidate/facts phù hợp và giữ
   provenance khi có dữ liệu/agent lỗi không?
3. Kết quả có tái lập trên snapshot mẫu hiện tại không?

Nó **không** chứng minh chất lượng trên dữ liệu marketplace thật, không đo
semantic correctness tự do và chưa so sánh chất lượng model với single-agent.

## 2. Frozen artifacts

- Corpus: [`evaluation/cases.v1.json`](../evaluation/cases.v1.json)
- Baseline manifest:
  [`evaluation/baselines/single_agent.v1.json`](../evaluation/baselines/single_agent.v1.json)
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
- latency không gồm inference/network production;
- deterministic v1 runtime quan sát 0 model calls, nhưng token/cost production
  được ghi **N/A**, không ghi 0.

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

## 7. Baseline honesty

Phase 1 hiện có hai fake-provider traces phục vụ tool-loop regression và một
integration test tùy chọn. Chúng không tạo thành model baseline vì thiếu:

- đủ 28 captured observations;
- model/version và generation settings;
- frozen prompt/tool-schema/data hashes;
- raw structured tool calls/results;
- provider token usage/cost;
- capture timestamp/runtime provenance.

Vì vậy manifest ghi `baseline_unavailable`, report không tính paired delta và
không thay baseline bằng scripted oracle. Khi thu thập đủ, phải lưu immutable
artifact, phân biệt explicit intent với intent suy từ tool, rồi mới report
single-vs-multi win/tie/loss.

## 8. Re-run

```powershell
ecommerce-evaluate --repeats 3 --output evaluation/results/latest
```

Smoke một phần corpus:

```powershell
ecommerce-evaluate --repeats 1 --max-cases 4 `
  --output evaluation/results/smoke
```

Outputs:

- `report.json`: metadata, global/category metrics, scores và observations;
- `observations.csv`: normalized per-run execution facts;
- `report.md`: bảng/giới hạn đọc được cho khóa luận.

Không overwrite `reference-v1` nếu chưa review diff, hash, all case scores và
runtime environment. Report mới với dataset/seed hash khác là một benchmark
version mới, không phải so sánh trực tiếp mặc định.

## 9. Mở rộng với dữ liệu thật

1. Freeze seed/data manifest và provenance policy mới.
2. Curator độc lập gắn gold facts/relevance; không dùng SUT sinh gold.
3. Thêm semantic/human evaluation rubric và inter-annotator agreement.
4. Thu frozen single-agent real-model baseline trên cùng corpus.
5. Chạy nhiều seed/model runs; tách correctness và latency repetitions.
6. Báo Wilson interval/paired win-tie-loss; không claim significance với sample
   nhỏ.
7. Bổ sung load, chaos, drift, fairness và PII/security evaluation.
