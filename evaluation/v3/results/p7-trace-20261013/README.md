# Trace pilot P7 r5–r7 — chỉ dùng development

Gói này giữ checkpoint, schedule, protocol, repeat decision, successor binding và đúng script chạy của ba pilot. r6 có thêm `pilot-reference-cases.p8.json`; đó là packet gồm output P7 dùng cho hiệu chuẩn judge về sau, không phải kết quả chấm P8.

Mỗi lượt có 36 cell: 4 warmup và 32 đo trên 8 ca development × 4 nhánh. Cả r5, r6, r7 đều hoàn thành 36/36. Hash của từng execution case trong cả ba run khớp 8/8 case pilot hiện tại. Hash gold/split toàn bộ protocol cũ hơn roster hiện tại, vì vậy cần giữ caveat cấp protocol.

## Phát hiện từ trace

- Ở `dev_shopping_merchant_01`, r5 ghi `ma_adaptive_rag` trả `action_mode_mismatch`; r6 và r7 ghi trả thẻ checkout chờ xác nhận. Hành vi này phù hợp với planner guard hiện có trong commit `e2d1f8b`. Đây chưa phải kiểm định nhân quả: protocol assets thay đổi giữa các run và checkpoint không ghi Git SHA của SUT.
- Trên 32 lượt đo r7, Adaptive RAG và SA đều có 6 `answered`, 1 `needs_clarification`, 1 `awaiting_confirmation`. Adaptive RAG dùng 40 generation calls, `$0.077554`, p50 `10.883s`; SA dùng 35 calls, `$0.070811`, p50 `9.406s`. Đây là outcome, chi phí và latency mô tả trên 8 ca, không phải điểm đúng ngữ nghĩa hay CI.
- Ở `dev_multi_expert_01`, cả Adaptive RAG và SA đều trả một bản liệt kê evidence dài 1.803 ký tự (thứ tự các đoạn nguồn khác nhau), không có câu so sánh trực tiếp hay khuyến nghị dù prompt yêu cầu. Cả hai có cảnh báo `answer_repair_not_grounded`. Grounding verifier hiện từ chối claim có phép so sánh; claim fact lại được render từ giá trị theo từng subject. Đây là giới hạn tổng hợp câu trả lời dùng chung giữa các nhánh, chưa chứng minh lỗi riêng của MA.

## Giới hạn và tái lập

`archive-manifest.json` ghi SHA-256 của file, schedule/protocol hash, số cell và so khớp hash từng case với gold hiện tại. Gold/split toàn bộ trong protocol là `f6988415…` / `d4af32e7…`, khác hash hiện tại `31bcb31c…` / `49adf66b…`; 8 case được chạy vẫn khớp từng hash. Git SHA của SUT không có trong trace nên còn **NOT VERIFIED**. Gói này không bao gồm judge P8, đánh giá người, hoặc lifecycle chính thức P7 → P8 → operate.
