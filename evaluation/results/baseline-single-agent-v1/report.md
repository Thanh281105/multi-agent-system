# Single-Agent Real-Model Baseline v1

- Model: `gpt-5.4-mini`
- Main revision: `c7b17cf`
- Dataset: `sample_ecommerce_vi_28_v1`
- Dataset SHA-256: `a34473eff4a6f7e87034b46c239833c852b12542ea4c0faea4b651ddb801671d`
- Captured cases: `28`
- Artifact SHA-256: `6fdf0f6ddb322c7778e1646bada25c17389144e411d9a5f38b98c18a986db328`

Đây là baseline single-agent chạy bằng API thật trên cùng frozen corpus.
Không dùng artifact này để claim hơn-kém nếu chưa có paired run cùng model,
prompt, runtime và rubric.

## Metrics

| Metric | Value | Numerator/denominator |
| --- | ---: | ---: |
| api_turn_success_rate | 1.0000 | 28/28 |
| task_success_rate_with_frozen_assertions | 0.0000 | 0/28 |
| task_success_rate_without_provenance_assertions | 0.0714 | 2/28 |
| answer_assertion_accuracy | 0.1279 | 11/86 |
| tool_selection_precision | 0.4762 | 20/42 |
| tool_selection_recall | 0.4348 | 20/46 |
| exact_plan_rate | 0.5000 | 14/28 |
| no_tool_correctness | 1.0000 | 4/4 |
| retrieval_precision | 0.7941 | 27/34 |
| retrieval_recall | 0.8710 | 27/31 |
| provenance_case_coverage | 0.0000 | 0/24 |
| latency_p50_ms | 3405.0610 |  |
| latency_p95_ms | 8474.6480 |  |
| token_usage | 58878 |  |
| llm_cost_usd | N/A | Provider pricing was not captured; no cost estimate is claimed. |

## Limitations

- Đây là một lần chạy real-model trên sample data, không phải human semantic evaluation.
- Single-agent main không phát provenance nên provenance coverage bằng 0 theo rubric Multi-Agent.
- Failure-injection cases được gửi như prompt bình thường; không thể ép main single-agent vào cùng failure dispatcher.
- Không ghi chi phí vì pricing/provider billing không được capture.
- Không suy diễn routing intent ẩn từ tool calls.
