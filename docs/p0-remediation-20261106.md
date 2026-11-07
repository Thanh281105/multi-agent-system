# Đợt sửa P0 — 2026-11-06

Phạm vi: tính trung thực của báo cáo và khả năng kiểm tra bằng chứng. Chưa sửa
runtime `app/v2`, corpus, model hoặc frozen protocol; không gọi provider.

## Đã thay đổi

- Thay hai script sinh trade-off/ablation từ hằng số bằng lệnh xuất gói v3 đã
  có, dùng lại validator hiện hành, yêu cầu đầu vào/output rõ ràng, không ghi
  đè. Giữ kết quả thua và metric chưa đo; không tự tuyên bố nhân quả.
- Thay script calibration có nhãn mô phỏng bằng bộ tổng hợp phiếu đã nộp.
  Packet được kiểm tra hash, phiếu phải nối đúng answer/metric và reviewer;
  số đồng thuận, tỷ lệ và confusion matrix đều tính từ đầu vào. Output mô
  tả không thay thế calibration freeze của P8 hay xác nhận người chấm thật.
- Sửa lỗi `compute_ablation_delta`: delta lớn hơn 5 pp không còn được coi là
  có ý nghĩa thống kê. Kiểm tra số đo, cặp cấu hình, và để tỷ số mẫu số 0 là null.
- Thêm audit manifest theo byte và phục hồi LF/CRLF sang thư mục mới, chỉ
  chấp nhận khi khớp chính xác hash và kích thước frozen. Không thay manifest.
- Đính chính báo cáo lịch sử và sửa các liên kết trỏ tới artifact local không
  có trong ZIP. Lệnh mới được mô tả tại [evidence-reporting.md](evidence-reporting.md).

## Artifact đã kiểm tra

Ba file catalog có thể phục hồi chính xác về CRLF. Bản phục hồi nằm tại
`output/evidence-recovery/catalog-20261106`; nguồn frozen ban đầu giữ nguyên.
Audit tại `output/evidence-audit/catalog-recovery-20261106.json` ghi riêng
source invalid và recovery valid. Đây là phục hồi byte, không xác nhận thời
điểm đăng ký trước, tính độc lập của tập hoặc chất lượng kết quả.

CSV P8 cũng phục hồi được bằng CRLF, nhưng cả gói vẫn bị từ chối phục hồi vì
thiếu `preparation.json`. Không tạo dữ liệu thay thế. Chi tiết tại
`output/evidence-audit/p8-20261106.json`.

## Kiểm tra đã thực hiện

- Python 3.12.16; dependencies từ `requirements-dev.lock`.
- Tái hiện trước sửa: 5 regression assertions thất bại ở hàm ablation.
- Sau sửa: `tests/test_evaluation_evidence_integrity.py` và
  `tests/test_documentation.py`: **36 passed**.
- Ruff check/format trên 9 file Python thay đổi: pass.
- Mypy trên 4 module evaluation mới/sửa: pass.
- Frontend build từ lockfile: pass; không có thay đổi giao diện.
- Audit catalog phục hồi: valid. P8: invalid đúng như trạng thái bằng chứng.

Toàn repo chưa xanh: Ruff còn 413 lỗi cũ (bản ZIP gốc 522, không có chữ ký lỗi
mới); mypy còn đúng 17 lỗi trong cùng 4 file có sẵn ở bản gốc.

Đối chiếu bản ZIP gốc tái hiện 6 lỗi ở `test_evaluation_v3_artifacts.py` do
frozen config dùng reasoning `low/medium`, còn schema nhận `none`. Cùng 6 lỗi
vẫn xảy ra sau sửa; hai lỗi link tài liệu trong baseline đã được sửa.

Bộ offline rộng chưa hoàn tất. Lần đầu thiếu frontend assets; sau build, môi
trường sandbox kẹt khi asyncio tạo socketpair. Lần chạy ngoài sandbox tiến
được tới khoảng 21%, gặp nhiều lỗi có sẵn và được dừng; không tuyên bố full
suite pass. Các tiến trình test của tác vụ đã được dừng. PostgreSQL, live
provider, HTTP/SSE/cancel và load test chưa được xác minh trong đợt này.

## Việc còn lại

Khôi phục preparation P8 từ nguồn lưu trữ hoặc tạo lần chạy/binding mới; xử lý
compatibility của protocol/schema bằng quyết định version rõ ràng; thu phiếu
người thật và tập xác nhận mới. Sau đó đánh giá cùng public runtime và baseline
đủ mạnh trước khi sửa routing, retrieval, grounding hoặc concurrency.
