# Hiệu chuẩn nhãn tham chiếu LLM — chỉ dùng development

Gói này lưu 32 nhãn tham chiếu do `gpt-6-luna` tạo cho các observation P8 thuộc
development. Đây không phải tập xác nhận mới, ground truth do người gán nhãn,
hay bằng chứng kiến trúc nào chính xác hơn.

Lượt chạy dùng `store=false`, gửi 32 request, chi phí ước tính `$0.0064505`,
thấp hơn cap `$0.25` đã duyệt. Packet request, reference bundle, provider audit,
script đúng phiên bản đã chạy và hash dạng portable đều có trong thư mục. Các
bản JSON/text rời dùng LF; `raw-calibration-capture-20261013.zip` giữ byte gốc để
kiểm tra lại hash request/response trong `run-manifest.json`.
`archive-manifest.json` ràng buộc bốn file portable và hash của raw capture.

Model tham chiếu khác model SUT/judge `gpt-5.4-mini-2026-03-17`, nên có thể dùng
để đo mức đồng thuận giữa hai model. Điều này không thay thế hiệu chuẩn với
người chấm: đồng thuận giữa LLM không phải xác nhận độc lập của con người. 752
lượt judge P8 và phép so sánh với các nhãn tham chiếu này chưa được chạy trong
artifact này.

Muốn tái tạo các request, dùng packet input và bản script đã lưu bên dưới; chạy
lại sẽ phát sinh phí API mới:

```powershell
.\.venv\Scripts\python.exe evaluation/v3/thesis-validation-v1/dev-judge-calibration-20261013/build_llm_reference_bundle.py `
  --cases evaluation/v3/thesis-validation-v1/dev-judge-calibration-20261013/input-cases.json `
  --output output/evaluation-v3/reproduced-reference-calibration/reference-labels.p8.json `
  --manifest output/evaluation-v3/reproduced-reference-calibration/run-manifest.json
```
