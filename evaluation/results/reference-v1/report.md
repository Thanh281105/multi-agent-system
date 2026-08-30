# Báo cáo benchmark offline multi-agent

- SUT source manifest SHA-256: `c02f2550d867eaa30ad06750c8379a7a09733c61b945cf61de87708992ff5437` (120 files)
- Dataset: `tiki_books_vi_28_v1` (`1838b7c5f3d43ef07e3595fcc507f755668b6330ff2d18bf9d7a8902fd2a2650`)
- Snapshot provenance: `data/snapshots/tiki-books-v4-eval` (`986803ba95d268cf158f36103efa2e1ce00c6134b0e03b67d96c7058f019d66d`)
- Manifest SHA-256: `e4f6580e33aa458713842855a7bb58b0e9a73942d888ff124ba4d30fd6513e8e`
- Quality report SHA-256: `b75c8e0f6efb8da8278b4d3babd33ffb9fabdcf5b58fea5678be90de4c537eac`
- Số case: 28; số lần lặp: 3
- Runtime: Python 3.12.10 trên `Windows-11-10.0.26200-SP0`
- Dữ liệu: **snapshot lịch sử Tiki Books đã làm sạch**, không phải dữ liệu Tiki trực tiếp hay ảnh chụp thị trường hiện tại
- Trạng thái so sánh baseline: `scripted_regression_only`

## Kết quả tổng hợp

| Metric | Giá trị | Mẫu | Ghi chú |
| --- | ---: | ---: | --- |
| routing_accuracy | 1.0000 | 28 | 28/28 ratio |
| tool_selection_precision | 1.0000 | 28 | 47/47 ratio |
| tool_selection_recall | 1.0000 | 28 | 47/47 ratio |
| tool_selection_f1 | 1.0000 | 28 | ratio |
| exact_plan_rate | 1.0000 | 28 | 28/28 ratio |
| no_tool_correctness | 1.0000 | 4 | 4/4 ratio |
| task_success_rate | 1.0000 | 28 | 28/28 ratio |
| answer_assertion_accuracy | 1.0000 | 96 | 96/96 ratio |
| retrieval_precision | 1.0000 | 26 | 31/31 ratio |
| retrieval_recall | 1.0000 | 26 | 31/31 ratio |
| retrieval_f1 | 1.0000 | 26 | ratio |
| empty_retrieval_correctness | 1.0000 | 11 | 11/11 ratio |
| agent_failure_rate | 0.1702 | 28 | 8/47 ratio |
| partial_recovery_rate | 1.0000 | 2 | 2/2 ratio |
| provenance_case_coverage | 1.0000 | 23 | 23/23 ratio |
| offline_latency_p50_ms | 3.4403 | 84 | ms |
| offline_latency_p95_ms | 29.1319 | 84 | ms |
| token_usage | N/A | 84 | offline deterministic runtime made zero observed model calls; production token usage was not measured |
| llm_cost_usd | N/A | 84 | offline deterministic runtime made zero observed model calls; production provider cost was not measured |

## Tính trung thực của baseline

Baseline v1 là mốc hồi quy deterministic, không phải bằng chứng từ mô hình. So sánh cặp có ý nghĩa dùng biến thể product-only deterministic trong protocol v2.

Không dùng scripted oracle để thay thế kết quả của một mô hình single-agent thật.

## Giới hạn

- Benchmark dùng snapshot lịch sử Tiki Books đã làm sạch gồm 200 sách và 1.773 review; đây không phải dữ liệu Tiki trực tiếp.
- Answer Accuracy là độ chính xác assertion có cấu trúc, không phải đánh giá ngữ nghĩa tự do.
- Latency là thời gian chạy local/offline, không đại diện suy luận LLM hay hạ tầng production.
- Baseline v1 chỉ là scripted regression; so sánh cặp deterministic được thực hiện trong protocol v2.
- Failure injection đo khả năng cô lập lỗi có chủ đích, không mô phỏng phân phối outage thực tế.
- Agent Failure Rate gồm cả lỗi dependency mong đợi ở case thiếu dữ liệu và lỗi được inject; đây không phải incident rate production.

## Kết quả từng case

- `simple_01_tarot_search` (simple): task=pass, route=pass, plan=pass
- `simple_02_quan_vuong_search` (simple): task=pass, route=pass, plan=pass
- `simple_03_eat_clean_search` (simple): task=pass, route=pass, plan=pass
- `simple_04_tarot_review` (simple): task=pass, route=pass, plan=pass
- `complex_01_compare_history` (complex): task=pass, route=pass, plan=pass
- `complex_02_compare_python` (complex): task=pass, route=pass, plan=pass
- `complex_03_rank_programming` (complex): task=pass, route=pass, plan=pass
- `complex_04_market_programming` (complex): task=pass, route=pass, plan=pass
- `multi_domain_01_budget_books` (multi_domain): task=pass, route=pass, plan=pass
- `multi_domain_02_political_theory` (multi_domain): task=pass, route=pass, plan=pass
- `multi_domain_03_author_filter` (multi_domain): task=pass, route=pass, plan=pass
- `multi_domain_04_publisher_pages` (multi_domain): task=pass, route=pass, plan=pass
- `missing_data_01_review` (missing_data): task=pass, route=pass, plan=pass
- `missing_data_02_complaint` (missing_data): task=pass, route=pass, plan=pass
- `missing_data_03_compare` (missing_data): task=pass, route=pass, plan=pass
- `missing_data_04_filter` (missing_data): task=pass, route=pass, plan=pass
- `tool_failure_01_trust` (tool_failure): task=pass, route=pass, plan=pass
- `tool_failure_02_review` (tool_failure): task=pass, route=pass, plan=pass
- `tool_failure_03_review_summary` (tool_failure): task=pass, route=pass, plan=pass
- `tool_failure_04_product` (tool_failure): task=pass, route=pass, plan=pass
- `ambiguous_01_reference` (ambiguous): task=pass, route=pass, plan=pass
- `ambiguous_02_price` (ambiguous): task=pass, route=pass, plan=pass
- `ambiguous_03_best` (ambiguous): task=pass, route=pass, plan=pass
- `ambiguous_04_complaint` (ambiguous): task=pass, route=pass, plan=pass
- `irrelevant_01_poem` (irrelevant): task=pass, route=pass, plan=pass
- `irrelevant_02_weather` (irrelevant): task=pass, route=pass, plan=pass
- `irrelevant_03_headphones` (irrelevant): task=pass, route=pass, plan=pass
- `irrelevant_04_algebra` (irrelevant): task=pass, route=pass, plan=pass
