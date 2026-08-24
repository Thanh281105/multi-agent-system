# Báo cáo benchmark offline multi-agent

- SUT source manifest SHA-256: `7c396e1176eea4c899fb96e6fe41c4489123df97602eed261ef824dbc5f8f75c` (94 files)
- Dataset: `sample_ecommerce_vi_28_v1` (`a34473eff4a6f7e87034b46c239833c852b12542ea4c0faea4b651ddb801671d`)
- Seed source SHA-256: `8dde4cd540226eb54b457cbb2271bb8b235711d9bdc8864ed840174c346a15d6`
- Số case: 28; số lần lặp: 3
- Runtime: Python 3.12.10 trên `Windows-11-10.0.26200-SP0`
- Dữ liệu: **mẫu tổng hợp**, không đại diện thị trường thật
- Trạng thái so sánh baseline: `real_model_captured`

## Kết quả tổng hợp

| Metric | Giá trị | Mẫu | Ghi chú |
| --- | ---: | ---: | --- |
| routing_accuracy | 1.0000 | 28 | 28/28 ratio |
| tool_selection_precision | 1.0000 | 28 | 46/46 ratio |
| tool_selection_recall | 1.0000 | 28 | 46/46 ratio |
| tool_selection_f1 | 1.0000 | 28 | ratio |
| exact_plan_rate | 1.0000 | 28 | 28/28 ratio |
| no_tool_correctness | 1.0000 | 4 | 4/4 ratio |
| task_success_rate | 1.0000 | 28 | 28/28 ratio |
| answer_assertion_accuracy | 1.0000 | 86 | 86/86 ratio |
| retrieval_precision | 1.0000 | 24 | 31/31 ratio |
| retrieval_recall | 1.0000 | 24 | 31/31 ratio |
| retrieval_f1 | 1.0000 | 24 | ratio |
| empty_retrieval_correctness | 1.0000 | 9 | 9/9 ratio |
| agent_failure_rate | 0.1739 | 28 | 8/46 ratio |
| partial_recovery_rate | 1.0000 | 2 | 2/2 ratio |
| provenance_case_coverage | 1.0000 | 23 | 23/23 ratio |
| offline_latency_p50_ms | 2.0422 | 84 | ms |
| offline_latency_p95_ms | 19.4387 | 84 | ms |
| token_usage | N/A | 84 | offline deterministic runtime made zero observed model calls; production token usage was not measured |
| llm_cost_usd | N/A | 84 | offline deterministic runtime made zero observed model calls; production provider cost was not measured |

## Tính trung thực của baseline

Đã chạy đủ 28 case trên main single-agent bằng OpenAI Responses API thật với model gpt-5.4-mini, cùng frozen corpus sample_ecommerce_vi_28_v1. Artifact ghi output, tool calls, token usage và latency; chưa claim paired hơn-kém vì multi-agent reference chạy deterministic offline khác runtime/provider.

Không dùng scripted oracle để thay thế kết quả của một mô hình single-agent thật.

## Giới hạn

- Benchmark chỉ dùng 30 sản phẩm và 150 review tổng hợp có gắn nhãn mẫu.
- Answer Accuracy là độ chính xác assertion có cấu trúc, không phải đánh giá ngữ nghĩa tự do.
- Latency là thời gian chạy local/offline, không đại diện suy luận LLM hay hạ tầng production.
- Baseline real-model được capture riêng; runner offline chưa thực hiện paired comparison vì không chạy cùng runtime/provider.
- Failure injection đo khả năng cô lập lỗi có chủ đích, không mô phỏng phân phối outage thực tế.
- Agent Failure Rate gồm cả lỗi dependency mong đợi ở case thiếu dữ liệu và lỗi được inject; đây không phải incident rate production.

## Kết quả từng case

- `simple_01_nova_search` (simple): task=pass, route=pass, plan=pass
- `simple_02_voltpro_search` (simple): task=pass, route=pass, plan=pass
- `simple_03_sunglow_search` (simple): task=pass, route=pass, plan=pass
- `simple_04_review_by_id` (simple): task=pass, route=pass, plan=pass
- `complex_01_compare_headphones` (complex): task=pass, route=pass, plan=pass
- `complex_02_compare_laptops` (complex): task=pass, route=pass, plan=pass
- `complex_03_rank_laptops` (complex): task=pass, route=pass, plan=pass
- `complex_04_market_headphones` (complex): task=pass, route=pass, plan=pass
- `multi_domain_01_headphones` (multi_domain): task=pass, route=pass, plan=pass
- `multi_domain_02_phones` (multi_domain): task=pass, route=pass, plan=pass
- `multi_domain_03_laptops` (multi_domain): task=pass, route=pass, plan=pass
- `multi_domain_04_accessories` (multi_domain): task=pass, route=pass, plan=pass
- `missing_data_01_review` (missing_data): task=pass, route=pass, plan=pass
- `missing_data_02_complaint` (missing_data): task=pass, route=pass, plan=pass
- `missing_data_03_compare` (missing_data): task=pass, route=pass, plan=pass
- `missing_data_04_filter` (missing_data): task=pass, route=pass, plan=pass
- `tool_failure_01_trust` (tool_failure): task=pass, route=pass, plan=pass
- `tool_failure_02_review` (tool_failure): task=pass, route=pass, plan=pass
- `tool_failure_03_review_summary` (tool_failure): task=pass, route=pass, plan=pass
- `tool_failure_04_product` (tool_failure): task=pass, route=pass, plan=pass
- `ambiguous_01_product` (ambiguous): task=pass, route=pass, plan=pass
- `ambiguous_02_price` (ambiguous): task=pass, route=pass, plan=pass
- `ambiguous_03_best` (ambiguous): task=pass, route=pass, plan=pass
- `ambiguous_04_complaint` (ambiguous): task=pass, route=pass, plan=pass
- `irrelevant_01_poem` (irrelevant): task=pass, route=pass, plan=pass
- `irrelevant_02_weather` (irrelevant): task=pass, route=pass, plan=pass
- `irrelevant_03_algebra` (irrelevant): task=pass, route=pass, plan=pass
- `irrelevant_04_translation` (irrelevant): task=pass, route=pass, plan=pass
