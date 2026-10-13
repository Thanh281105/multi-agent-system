# Hiệu chuẩn nhãn tham chiếu LLM — chỉ dùng development

Gói này lưu 32 nhãn tham chiếu do `gpt-6-luna` tạo cho các observation đo của
pilot P7 (`pilot-live-v3`: 8 ca development × 4 nhánh), để hiệu chuẩn judge P8
sau này. Tên file `reference-labels.p8.json` không có nghĩa đây là câu trả lời
P8 đã được chấm. Đây không phải tập xác nhận mới, ground truth do người gán
nhãn, hay bằng chứng kiến trúc nào chính xác hơn.

Lượt chạy dùng `store=false`, gửi 32 request, chi phí ước tính `$0.0064505`,
thấp hơn cap `$0.25` đã duyệt. Packet request, reference bundle, provider audit,
script đúng phiên bản đã chạy và hash dạng portable đều có trong thư mục. Các
bản JSON/text rời dùng LF; `raw-calibration-capture-20261013.zip` giữ byte gốc để
kiểm tra lại hash request/response trong `run-manifest.json`.
`archive-manifest.json` ràng buộc bốn file portable và hash của raw capture.

Model tham chiếu khác model SUT/judge `gpt-5.4-mini-2026-03-17`, nên có thể dùng
để đo mức đồng thuận giữa hai model. Kiểm tra trạng thái ngày 2026-10-13 thấy
32/32 job calibration development và 720/720 job P8 held-out đã hoàn tất trong
`output/evaluation-v3/heldout-live-v3`. Calibration freeze ghi nhận bất đồng
16/49 nhãn claim (32,65%), với ngưỡng lỗi đúng bằng tỷ lệ này. Đây không phải
hiệu chuẩn với người chấm; đồng thuận giữa LLM không xác nhận độ đúng ngữ nghĩa.
Các output P8 nằm trong thư mục bị `.gitignore` loại khỏi checkout sạch, dùng
judge cùng model với SUT và thuộc split held-out đã được dùng remediation.

Nguồn được xác minh bằng cách ghép 32 observation ID và receipt SHA-256 với
checkpoint `output/evaluation-v3/pilot-live-v3/pilot-checkpoint.v3.jsonl`;
schedule có 8 ca × 4 nhánh đo, không gồm warmup. Tuy nhiên, protocol P7 trong
artifact bind gold `f6988415…`, khác hash `31bcb31c…` của
`evaluation/v3/gold.v3.json` hiện tại; hash split cũng khác (`d4af32e7…` so với
`49adf66b…`). Runtime manifest bind `source_head=9adbf74c74bed0439ba317cc3956c9dea1c99249`
(xác minh 2026-10-04), cũ hơn các thay đổi `app/v2` hiện tại. Vì vậy các trace
này không xác nhận hành vi của source hiện tại. Không gửi lại các request chỉ
để tái tạo chúng; lần chạy P7 mới phải dùng đúng source snapshot đã khóa.

Muốn tái tạo các request, dùng packet input và bản script đã lưu bên dưới; chạy
lại sẽ phát sinh phí API mới:

```powershell
.\.venv\Scripts\python.exe evaluation/v3/thesis-validation-v1/dev-judge-calibration-20261013/build_llm_reference_bundle.py `
  --cases evaluation/v3/thesis-validation-v1/dev-judge-calibration-20261013/input-cases.json `
  --output output/evaluation-v3/reproduced-reference-calibration/reference-labels.p8.json `
  --manifest output/evaluation-v3/reproduced-reference-calibration/run-manifest.json
```
