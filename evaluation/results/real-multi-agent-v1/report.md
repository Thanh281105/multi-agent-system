# Multi-Agent Real-Model Benchmark v1

- Model: `gpt-5.4-mini`
- Protocol: `deterministic multi-agent evidence + real-model final synthesis`
- Dataset: `sample_ecommerce_vi_28_v1` (`a34473eff4a6f7e87034b46c239833c852b12542ea4c0faea4b651ddb801671d`)
- Cases: `28` × `1`
- Artifact SHA-256: `3ec1d43c6a37bf19d9786c52a89772fdcab2c67ca1d9e85e4cad0d9f6015955e`

## Metrics

| Metric | Value | Sample/denominator |
| --- | ---: | ---: |
| routing_accuracy | 1.0000 | 28.0/28.0 |
| tool_selection_precision | 1.0000 | 46.0/46.0 |
| tool_selection_recall | 1.0000 | 46.0/46.0 |
| tool_selection_f1 | 1.0000 | 28 |
| exact_plan_rate | 1.0000 | 28.0/28.0 |
| no_tool_correctness | 1.0000 | 4.0/4.0 |
| task_success_rate | 1.0000 | 28.0/28.0 |
| answer_assertion_accuracy | 1.0000 | 86.0/86.0 |
| retrieval_precision | 1.0000 | 31.0/31.0 |
| retrieval_recall | 1.0000 | 31.0/31.0 |
| retrieval_f1 | 1.0000 | 24 |
| empty_retrieval_correctness | 1.0000 | 9.0/9.0 |
| agent_failure_rate | 0.1739 | 8.0/46.0 |
| partial_recovery_rate | 1.0000 | 2.0/2.0 |
| provenance_case_coverage | 1.0000 | 23.0/23.0 |
| token_usage | 61778.0000 | 28 |
| llm_cost_usd | N/A | 28 |
| real_latency_p50_ms | 1167.5007 | 28 |
| real_latency_p95_ms | 2042.1254 | 28 |
| model_latency_p50_ms | 1153.3650 | 28 |
| model_latency_p95_ms | 2016.7990 | 28 |

## Descriptive comparison with single-agent main

| Metric | Multi-Agent real | Single-Agent main real | Delta (multi − single) |
| --- | ---: | ---: | ---: |
| Answer assertions | 1.0000 | 0.1279 | 0.8721 |
| Tool precision | 1.0000 | 0.4762 | 0.5238 |
| Tool recall | 1.0000 | 0.4348 | 0.5652 |
| Exact plan | 1.0000 | 0.5000 | 0.5000 |
| Retrieval precision | 1.0000 | 0.7941 | 0.2059 |
| Retrieval recall | 1.0000 | 0.8710 | 0.1290 |
| Provenance coverage | 1.0000 | 0.0000 | 1.0000 |
| Task success (frozen rubric) | 1.0000 | 0.0000 | 1.0000 |
| Latency p50 (ms) | 1167.5007 | 3405.0610 | -2237.5603 |
| Latency p95 (ms) | 2042.1254 | 8474.6480 | -6432.5226 |
| Token usage | 61778.0000 | 58878.0000 | 2900.0000 |

## Limitations

- Domain routing, planning và skills vẫn deterministic; real API chỉ tổng hợp câu trả lời cuối.
- Một lần chạy real-model không thay cho human semantic evaluation.
- Chi phí không ghi vì chưa capture pricing/provider billing.
- CI chỉ score artifact, không gọi API và không chứa secret.
