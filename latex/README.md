# Báo cáo hệ thống trợ lý tư vấn sách

- `bao-cao-thanh-v4.pdf`: báo cáo môn học viết để giảng viên hiểu bài toán, mô hình, cách phối hợp, kết quả và các cải tiến sau thực nghiệm; có trang bìa, lời cảm ơn, lời cam đoan và tóm tắt. Sơ đồ chỉ đen trắng, nhãn tiếng Việt.
- `main.tex`: tệp chính; các phần nội dung nằm trong `01-*.tex` đến `05-*.tex` và `sources.tex`.
- `images/`: nguồn HTML tự chứa, SVG và PNG của ba sơ đồ được tạo theo skill cathrynlavery/diagram-design.
- `kiem-chung-so-lieu.json`: số liệu độc lập tính từ các artifact đã commit, không gọi model.
- `ket-qua-v4-theo-ca.csv`: bảng từng ca, UTF-8 BOM để mở bằng Excel.
- `tools/audit_report.py`: mã Python chuẩn để tái tính, kiểm hash và completeness.

## Biên dịch

Yêu cầu Tectonic hoặc XeLaTeX, các gói trong preamble, Times New Roman, Arial và Consolas. Ảnh và văn bản đều có sẵn; không cần tải webfont. Lần chạy Tectonic đầu tiên có thể cần tải các gói TeX nếu cache chưa có.

Trên PowerShell, nếu `tectonic` nằm trong PATH hoặc có biến `CODEX_TECTONIC_PATH`:

```powershell
./Build.ps1
```

Hoặc chạy `xelatex main.tex` hai lần trong thư mục này. `main.tex` là main document của dự án LaTeX.

## Tái tính kiểm chứng

Trỏ đến checkout đúng commit `0a4930e87664dd91add96025a2230b92a15810e3` và một thư mục kết quả riêng:

```powershell
python tools/audit_report.py --repo 'C:/path/to/multi-agent-system' --out './audit-reproduced'
```

Script chỉ đọc checkout, ghi JSON/CSV vào thư mục output được chỉ định; không chạy dịch vụ, không gọi provider và không sửa repository.

Các số liệu được tính lại từ kết quả đã lưu, không phải một đợt chạy thực nghiệm mới. Thực nghiệm và các nguồn [1]–[6] gắn với commit `0a4930e`; phần cải tiến sau thực nghiệm (nguồn [7]–[9]) gắn với commit `0d83c79` trên nhánh `v4-evidence-clean`, nên chỉ mở được sau khi nhánh này được push lên GitHub. Lần chạy xác nhận (mục 8.6, nguồn [10]) gắn với commit `ebb3da8` trên cùng nhánh; số liệu của mục này tái tạo bằng `evaluation/v3/confirm-20261110/results/analyze.py`, không nằm trong `kiem-chung-so-lieu.json`. Bản PDF đã lược các chi tiết triển khai và tập trung giải thích bằng ngôn ngữ dễ hiểu; dữ liệu kiểm chứng vẫn được giữ riêng để có thể kiểm tra lại các phép tính.

## Skill thiết kế

Nguồn: https://github.com/cathrynlavery/diagram-design (MIT). Dùng bản cài sẵn 2.6.119. Người dùng chọn đen trắng; cấu hình cục bộ và các chi tiết lược khỏi hình được ghi trong `thiet-ke-so-do.md`.
