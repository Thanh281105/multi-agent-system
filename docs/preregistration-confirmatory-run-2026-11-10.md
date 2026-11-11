# Khai báo trước (pre-registration) cho lần chạy xác nhận — 2026-11-10

Tài liệu này được commit **trước** khi viết bộ câu hỏi mới và trước khi chạy hệ
thống. Mọi thay đổi sau commit này phải là commit riêng, ghi rõ lý do; kết quả
được báo cáo theo đúng các quy tắc dưới đây dù thuận hay nghịch cho multi-agent
(MA).

## Nguồn gốc giả thuyết và giới hạn

Giả thuyết đến từ phân tích phụ, thăm dò trên P8 (720 ô): `ma_fixed_rag` so với
`sa_shared_tools_rag` có Δ task_completion tổng thể +2,06 điểm %, CI 95%
[−3,0; +7,2]; ở loại `multi_constraint` +5,56, CI [0,0; +13,9]. `ma_adaptive_rag`
không hơn SA ở loại nào (tổng −3,81, CI [−8,8; +0,9]). Các tín hiệu trên chưa có
ý nghĩa thống kê và được chọn sau khi xem dữ liệu P8, nên lần chạy này chỉ có giá
trị xác nhận nếu giả thuyết được khóa trước như ở đây.

Giới hạn bắt buộc nêu trong báo cáo:
- Bộ câu hỏi mới dùng **cùng 13 sản phẩm và 27 nhóm tác phẩm** với P8 (pipeline
  hiện tại không cho phép sản phẩm khác). Đây là bằng chứng tái lập về cách hỏi
  mới, **không** phải holdout độc lập về dữ liệu.
- Câu hỏi và gold do một subagent Claude soạn theo schema, không được xem kết quả
  P8 hay lỗi hệ thống. Cùng họ model với bộ chấm và hệ thống nên không phải người
  viết độc lập như người thật.
- Bộ chấm là LLM judge, chưa có người hiệu chuẩn.

## Giả thuyết

- **H1 (chính, một phía):** trên loại câu `multi_constraint`, task_completion của
  `ma_fixed_rag` cao hơn `sa_shared_tools_rag`.
- **H2 (phụ):** trên toàn bộ 60 câu held-out mới, task_completion của
  `ma_fixed_rag` cao hơn `sa_shared_tools_rag`.
- **H3 (phụ, ghi nhận):** so sánh `ma_adaptive_rag` với SA, cùng cách tính. Không
  có kỳ vọng hướng.

Kết luận "ủng hộ H1" chỉ được nêu nếu CI 95% bootstrap theo work group của Δ
không chứa 0 và Δ > 0. Mọi trường hợp khác báo là **không ủng hộ / không kết
luận**, kèm số đo.

## Quy tắc phân tích (khóa trước)

- Chỉ số: task_completion do judge `gpt-6-luna` chấm mù, theo cấu hình
  `p8` hiện có, không đổi judge, prompt hay ngưỡng sau khi xem điểm.
- Δ = trung bình theo work group của (biến thể − SA) trên trung bình các lần lặp
  của từng hội thoại; bootstrap 10.000 mẫu theo work group, seed 42.
- Báo cáo đủ W/T/L, mọi loại câu và cả `claim_support`, kể cả khi bất lợi cho MA.
- Không loại ô, không chạy lại ô vì điểm thấp. Ô lỗi kỹ thuật xử lý theo quy tắc
  sẵn có của pipeline và được liệt kê.
- Số loại câu ngoài H1 là phân tích phụ, đa so sánh, chỉ mô tả.
- Câu hỏi được đóng băng bằng hash trước khi chạy; không sửa câu hỏi/gold sau
  khi chạy.
- Ngân sách tối đa 4 USD cho lần chạy (đã nằm trong 10 USD được duyệt); vượt thì
  dừng và báo.
