# Chẩn đoán trace MA `post16`

## Phạm vi bằng chứng

Đây là trace development để tìm lỗi vận hành, không phải kết quả xác nhận chất lượng. Artifact cục bộ `output/evaluation-v3/ma-trace-20261012-post16` có 32/32 lượt đo (8 mỗi arm), protocol SHA-256 `580e07f88fd9784ff4b42fe9270c2089f033bbc36b1e2effdcf4157c124dc5d1` và checkpoint SHA-256 `a4150784ff9335a2c70b8d9f39d2f91b5c9013708cc956e4c22ec7660ece5a81`. Protocol không ghi Git SHA của SUT, nên chưa xác minh trace này có chạy đúng source hiện tại hay không.

32 nhãn tham chiếu `gpt-6-luna` trong `dev-judge-calibration-20261013` thuộc run `pilot-live-v3`, không thuộc `post16`: 32 observation ID của hai gói không giao nhau. Không dùng các nhãn đó để kết luận chất lượng `post16`.

## Trace ca so sánh hai sách

Ở `dev_multi_expert_01`, các RAG arm có cùng 6 evidence ID cuối cùng. SA và Fixed MA có cùng answer hash; Adaptive MA có câu trả lời khác nhưng vẫn dài 1.803 ký tự và có 6 claims. Chưa có nhãn judge đúng với run này để xác định khác biệt câu chữ có tốt hơn không.

| Arm | Latency | Generation calls | Input/output tokens | Chi phí đã ghi |
| --- | ---: | ---: | ---: | ---: |
| SA + RAG | 23,81 giây | 6 | 9.008 / 2.500 | $0,01413 |
| Fixed MA + RAG | 31,47 giây | 8 | 12.254 / 3.006 | $0,01797 |
| Adaptive MA + RAG | 59,31 giây | 10 | 21.352 / 6.586 | $0,04060 |

Fixed và Adaptive MA mỗi nhánh gọi hai `product_expert` và một `knowledge_expert`; SA không có các lượt specialist. Kế hoạch đọc của ca gồm `product.catalog.search`, `product.compare`, `knowledge.retrieve`. Trong `app/v2`, `product.compare` trả facts có cấu trúc từ cặp sản phẩm đã chọn; selector chỉ có thể chọn/bỏ toàn bộ nhóm fact theo chủ thể. `select_grounding_evidence` khôi phục toàn bộ evidence cho obligation so sánh tường minh. Với trace này, tập evidence cuối cùng cũng không khác giữa ba RAG arm. Điều đó làm lượt selector của `product.compare` thành ứng viên bỏ được để giảm chi phí; trace không chứng minh phần chữ khác nhau do selector này.

Ca `dev_knowledge_source_01` là lỗi chung ở answer pipeline: cả ba RAG arm trả lại đoạn nguồn markdown dài 528 ký tự và có cảnh báo `answer_repair_not_grounded`. Nhãn tham chiếu không khớp run nên đây chỉ là tín hiệu cần giữ lại cho development benchmark kế tiếp, chưa phải nhãn đúng/sai.

## Thay đổi ứng viên và cổng xác nhận

Source hiện bỏ lượt specialist selector trên capability `product.compare`; selector vẫn hoạt động cho các capability khác, gồm truy hồi tri thức dạng văn bản. Mục tiêu là bỏ một lần gọi không thêm evidence cho phép so sánh có cấu trúc, không khẳng định cải thiện correctness. Cần đo lại trên đúng commit `app/v2`: P7 trước, P8 chính thức đủ 720/720, rồi operate trên cùng binding. Chưa có kết quả lifecycle nào xác nhận thay đổi này.
