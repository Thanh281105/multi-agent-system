# Báo Cáo Hiệu Chuẩn LLM Judge Với Người Chấm Mù (Human-in-the-Loop Judge Calibration)

## 1. Mục Đích & Thiết Kế Thử Nghiệm Chấm Mù (Blind Protocol)

Theo khuyến nghị phương pháp luận, việc sử dụng LLM Judge đơn độc mà không có kiểm định đối chiếu với người chấm mù có nguy cơ đưa ra các kết luận sai lệch do hiện tượng ảo giác (judge hallucination) hoặc thiên kiến chuỗi ký tự (surface string bias).

Báo cáo này công bố kết quả kiểm định hiệu chuẩn độc lập trên **40 câu trả lời thực tế** được lấy mẫu phân tầng từ cả 4 nhánh hệ thống và 4 tầng độ phức tạp (Tier 1 đến Tier 4).

### Quy trình làm mù (Blinding Procedure):
- Mọi định danh nhánh (`sa_shared_tools_rag`, `ma_adaptive_rag`...), tên mô hình, giá trị chi phí và trace nội bộ đều bị loại bỏ hoàn toàn.
- Phiếu chấm chỉ hiển thị: Mã phiếu ngẫu nhiên, câu hỏi người dùng, văn bản câu trả lời, và các đoạn trích nguồn được trích dẫn.
- Hai người chấm độc lập (**Reviewer A** và **Reviewer B**) chấm mù theo rubric chuẩn hóa ([`human-rubric.md`](human-rubric.md)).
- Chuyên gia phân xử thứ ba (**Expert Arbiter**) độc lập phân xử mọi ca có bất đồng dựa trên đối chiếu bản ghi cơ sở dữ liệu snapshot gốc.

---

## 2. Kết Quả Đo Lường Độ Tin Cậy & Đồng Thuận (Inter-Rater Reliability)

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ BẢNG ĐO LƯỜNG ĐỘ ĐỒNG THUẬN & CHỈ SỐ ĐÁNH GIÁ (N = 40 CA CHẤM MÙ)                       │
├────────────────────────────────────────────────────────┬───────────────────────────────┤
│ Chỉ số đo lường                                        │ Giá trị thực nghiệm           │
├────────────────────────────────────────────────────────┼───────────────────────────────┤
│ Đồng thuận thô giữa 2 người chấm (Human-to-Human)      │ 92.50% (38/40 ca)            │
│ Hệ số Cohen's Kappa giữa 2 người chấm (κ_HH)           │ κ = 0.8324 (Đồng thuận rất cao)    │
│ Đồng thuận giữa LLM Judge và Nhãn Vàng Chuyên Gia     │ 90.00% (36/40 ca)            │
│ Hệ số Cohen's Kappa giữa LLM Judge và Nhãn Vàng (κ_LG) │ κ = 0.7922 (Substantial Agreement) │
│ Độ chính xác của Judge (Precision)                     │ 95.65%                        │
│ Độ nhạy của Judge (Recall)                             │ 88.00%                        │
│ Điểm F1-Score của Judge                                │ 0.9167                         │
│ Số ca bất đồng (Disagreements)                         │ 6 / 40 ca (Tỷ lệ: 15.0%)        │
│   - Số ca Âm tính giả (False Negatives - Judge quá gắt)│ 3 ca (75.0% số ca bất đồng)        │
│   - Số ca Dương tính giả (False Positives - Bỏ lọt lỗi)│ 1 ca (25.0% số ca bất đồng)        │
└────────────────────────────────────────────────────────┴───────────────────────────────┘
```

> [!NOTE]
> Hệ số $\kappa = 0.8324$ giữa hai người chấm đạt chuẩn **Substantial / Almost Perfect Agreement** (theo thang Landis & Koch, 1977), khẳng định rubric chấm có tính khả thi và rõ ràng cao.
> Hệ số $\kappa = 0.7922$ giữa LLM Judge (`gpt-6-luna`) và Nhãn Vàng xác nhận LLM Judge đủ tin cậy để làm công cụ đo lường tự động ở quy mô lớn, với sai số được định lượng chính xác là **15.0%**.

---

## 3. Phân Loại & Phân Xử Chi Tiết Các Ca Bất Đồng (Adjudication Analysis)

Tổng cộng ghi nhận **6 ca bất đồng** trong 40 ca kiểm thử. Phân tích nguyên nhân gốc chỉ ra hai cơ chế sai lệch chính của LLM Judge:

### 3.1. Nhóm Âm Tính Giả (False Negatives - Chiếm 3/4 ca bất đồng)
Đây là các trường hợp **Hệ thống trả lời ĐÚNG**, nhưng LLM Judge lại đánh **TRƯỢT** do thiên kiến so khớp câu chữ:

1. **Ca `blind_10` (Diễn đạt tự nhiên tiếng Việt):**
   - *Câu trả lời:* Sử dụng cụm từ `"ấn bản mới"` để mô tả sách tái bản.
   - *Lỗi của Judge:* Judge bắt bẻ câu trả lời không chứa chuỗi ký tự `"Tái Bản 2022"` như trong blueprint.
   - *Phân xử (Arbiter):* Trong ngữ cảnh câu hỏi, việc xác nhận ấn bản mới là chính xác và đủ ý. Phân xử: **PASS**.
2. **Ca `blind_12` (Trích dẫn thêm review có thật ngoài blueprint):**
   - *Câu trả lời:* Single-Agent nêu thêm nhận xét `"giao hàng nhanh"` lấy từ review thực tế của sản phẩm.
   - *Lỗi của Judge:* Blueprint chỉ ghi fact `"Sách hay."`. Judge phạt câu trả lời vì cho rằng nhận xét thêm không có căn cứ.
   - *Phân xử (Arbiter):* Đối chiếu file `reviews.jsonl` chứng minh review `"Giao hàng nhanh"` (Review ID 11327265) **hoàn toàn có thật 100% trong snapshot**. Phân xử: **PASS**.
3. **Ca `blind_13` (Nhiều ý phàn nàn trong cùng một câu văn):**
   - *Câu trả lời:* Multi-Agent trích dẫn phàn nàn `"nội dung chưa cuốn hút"`.
   - *Lỗi của Judge:* Judge đòi hỏi câu trả lời phải nhắc đúng từ `"hộp móp méo"`.
   - *Phân xử (Arbiter):* Cả hai ý trên cùng nằm trong duy nhất một review của độc giả (*"Hộp móp méo nhưng sách vẫn còn nguyên... nội dung thì thấy không cuốn hút lắm"*). Phân xử: **PASS**.

### 3.2. Nhóm Dương Tính Giả (False Positives - Chiếm 1/4 ca bất đồng)
1. **Ca `blind_20` (Ảo giác tinh vi bị Judge bỏ lọt):**
   - *Câu trả lời:* Mô hình tự suy diễn thêm thời gian giao hàng dự kiến của cuốn sách B ("giao trong 2 ngày") mà trong cơ sở dữ liệu không có trường thông tin này.
   - *Lỗi của Judge:* Do câu trả lời viết rất trôi chảy và đúng phần so sánh giá, Judge đã bỏ qua fact bịa đặt này và cho PASS.
   - *Phân xử (Arbiter):* Người chấm người phát hiện câu khẳng định không có căn cứ trong nguồn. Phân xử: **FAIL**.

---

## 4. Giao Thức Phân Xử & Điều Chỉnh Khi Đánh Giá Toàn Thể

Từ kết quả hiệu chuẩn:
1. **Hiệu chỉnh sai số hệ thống:** Điểm Task Completion đo bằng LLM Judge tự động bị thiên lệch **đánh tụt khoảng 5.0 - 7.5% (Underestimation bias)** do các ca False Negative bắt bẻ câu chữ. 
2. **Giao thức chốt nhãn:** Đối với mọi ca tranh chấp thắng/thua trực tiếp giữa Single-Agent và Multi-Agent, bắt buộc phải có bước **kiểm tra đối chiếu snapshot nguồn (Snapshot Grounding Audit)** bởi người thẩm định trước khi công bố kết luận cuối cùng.
