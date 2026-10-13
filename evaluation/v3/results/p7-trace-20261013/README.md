# Trace pilot P7 r5–r7 — chỉ dùng development

Gói này giữ checkpoint, schedule, protocol, repeat decision, successor binding và đúng script chạy của ba pilot. r6 có thêm `pilot-reference-cases.p8.json`; đó là packet gồm output P7 dùng cho hiệu chuẩn judge về sau, không phải kết quả chấm P8.

Mỗi lượt có 36 cell: 4 warmup và 32 đo trên 8 ca development × 4 nhánh. Cả r5, r6, r7 đều hoàn thành 36/36. Hash của từng execution case trong cả ba run khớp 8/8 case pilot hiện tại. Hash gold/split toàn bộ protocol cũ hơn roster hiện tại, vì vậy cần giữ caveat cấp protocol.

## Phát hiện từ trace

- Ở `dev_shopping_merchant_01`, r5 ghi `ma_adaptive_rag` trả `action_mode_mismatch`; r6 và r7 ghi trả thẻ checkout chờ xác nhận. Hành vi này phù hợp với planner guard hiện có trong commit `e2d1f8b`, nhưng chưa phải kiểm định nhân quả vì protocol assets thay đổi giữa các run. Các protocol lần lượt ghim SUT SHA `f756c84f14f2efa8d73a4e6618e654f720f8c741` (r5), `e2d1f8b2b8dc5dd020ad4511d5aebd2216f46db3` (r6) và `4238394ee932dbf79a53aaa1a565c5203f857263` (r7); checkpoint không lặp lại trường này.
- Trên 32 lượt đo r7, Adaptive RAG và SA đều có 6 `answered`, 1 `needs_clarification`, 1 `awaiting_confirmation`. Adaptive RAG dùng 40 generation calls, `$0.077554`, p50 `10.883s`; SA dùng 35 calls, `$0.070811`, p50 `9.406s`. Đây là outcome, chi phí và latency mô tả trên 8 ca, không phải điểm đúng ngữ nghĩa hay CI.
- Ở `dev_multi_expert_01`, cả Adaptive RAG và SA đều trả một bản liệt kê evidence dài 1.803 ký tự (thứ tự các đoạn nguồn khác nhau), không có câu so sánh trực tiếp hay khuyến nghị dù prompt yêu cầu. Cả hai có cảnh báo `answer_repair_not_grounded`. Grounding verifier hiện từ chối claim có phép so sánh; claim fact lại được render từ giá trị theo từng subject. Đây là giới hạn tổng hợp câu trả lời dùng chung giữa các nhánh, chưa chứng minh lỗi riêng của MA.

## Giới hạn và tái lập

`archive-manifest.json` ghi SHA-256 của file, schedule/protocol hash, số cell và so khớp hash từng case với gold hiện tại. Gold/split toàn bộ trong protocol là `f6988415…` / `d4af32e7…`, khác hash hiện tại `31bcb31c…` / `49adf66b…`; 8 case được chạy vẫn khớp từng hash. SHA của SUT trong từng protocol đã xác định, nhưng source r7 chưa bao gồm các thay đổi runtime hiện tại ở `app/v2/answers.py`, `app/v2/execution.py` và `app/v2/runtime_contracts.py`. Vì vậy chưa có trace P7 xác nhận các thay đổi sau r7. Gói này không bao gồm judge P8, đánh giá người, hoặc lifecycle chính thức P7 → P8 → operate.

## Launcher cho P7 sau thay đổi source

`r8/run_p7_trace_r8.py` chạy pilot development 8 ca trên bộ `review-successor-v1`, dùng role `eval_runner` và kiểm tra `current_user`; protocol mới ghim SHA của HEAD. Pilot này chỉ để đọc trace. Launcher không tạo repeat decision hay successor binding vì split P8 cũ đã được dùng trong remediation; P8 cần một tập xác nhận mới được khóa riêng. Mỗi lần chạy cần `run_id` và output mới; launcher dừng nếu output r8 đã tồn tại. Chưa chạy: tại lần kiểm tra 2026-10-13, `127.0.0.1:55433` không có listener và Docker Desktop API từ chối quyền.
