# Báo cáo hệ thống trợ lý tư vấn sách

Báo cáo môn học Dự án Công nghệ thông tin: bài toán, cơ sở lý thuyết, dữ liệu, thiết kế hệ thống, cách đánh giá, kết quả ba đợt thực nghiệm, quá trình rà soát và kết luận.

## Các tệp

- `bao-cao-thanh-v4.pdf`: bản PDF để đọc và in (98 trang, khổ A4).
- `main.tex`: tệp chính (bìa, lời cảm ơn, lời cam đoan, tóm tắt, mục lục, rồi gọi các phần bên dưới).
- `preamble.tex`: định dạng chung (cỡ chữ, lề, kiểu bảng, kiểu hình).
- `thuat-ngu.tex`: danh mục thuật ngữ và từ viết tắt.
- `chuong-1-*.tex` đến `chuong-8-*.tex`: tám chương nội dung.
- `phu-luc.tex`: bốn phụ lục. `sources.tex`: tài liệu tham khảo.
- `figures/`: 16 hình vẽ bằng TikZ/pgfplots. `images/`: logo trường và ảnh chụp giao diện.
- `ket-qua-v4-theo-ca.csv`, `kiem-chung-so-lieu.json`, `tools/audit_report.py`: số liệu đợt 1 theo từng câu và công cụ tính lại từ kết quả đã lưu (không gọi mô hình).
- `kiem-tra-pdf.json`: biên bản kiểm tra tự động bản PDF (số trang, ký tự lỗi, tham chiếu chưa giải quyết, số trang ở chân trang).

## Định dạng

- Chữ thân bài Times New Roman 13pt, giãn dòng 1,3; lề trái 3 cm, phải 2 cm, trên và dưới 2,5 cm.
- Lớp `report` chỉ có sẵn cỡ 10–12pt, nên cỡ 13pt được đặt bằng gói `scrextend` trong `preamble.tex`.
- Hình chỉ dùng đen, trắng, xám và nét gạch nên in đen trắng vẫn phân biệt được. Chữ trong hình là Arial 8–9pt, cố định, không phụ thuộc cỡ chữ thân bài.
- Báo cáo không chứa mã nguồn; tên kỹ thuật trong chương trình được thay bằng tên gọi tiếng Việt (xem danh mục thuật ngữ).

## Biên dịch

Cần Tectonic hoặc XeLaTeX, cùng hai phông Times New Roman và Arial. Lần chạy Tectonic đầu tiên có thể phải tải các gói TeX.

Trên PowerShell, nếu `tectonic` nằm trong PATH hoặc có biến `CODEX_TECTONIC_PATH`:

```powershell
./Build.ps1
```

Lệnh này biên dịch vào thư mục `build/` rồi chép kết quả thành `bao-cao-thanh-v4.pdf`. Cũng có thể chạy `xelatex main.tex` ba lần trong thư mục này.

Khi sửa hình: mỗi hình là một tệp riêng trong `figures/`. Sau khi sửa nên mở lại trang chứa hình trong PDF để chắc chắn chữ không tràn khỏi ô và không đè lên nhau.

## Nguồn số liệu

Mọi con số trong báo cáo lấy từ các tài liệu và tệp kết quả đã lưu trong kho mã; danh sách đầy đủ nằm ở mục “Tài liệu và kết quả của dự án” trong phần Tài liệu tham khảo (các nguồn [14]–[23]). Báo cáo không chạy thêm thực nghiệm mới.

Số liệu đợt 1 có thể tính lại bằng:

```powershell
python tools/audit_report.py --repo 'C:/path/to/multi-agent-system' --out './audit-reproduced'
```

Công cụ chỉ đọc kho mã và ghi kết quả vào thư mục chỉ định; không chạy dịch vụ và không gọi mô hình. Số liệu đợt 3 tính lại bằng `evaluation/v3/confirm-20261110/results/analyze.py`.
