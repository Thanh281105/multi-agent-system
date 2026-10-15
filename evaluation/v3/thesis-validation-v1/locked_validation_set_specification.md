# Đặc Tả Bộ Xác Nhận Độc Lập Đã Khóa (Locked Independent Validation Set Specification)

## 1. Đặt Vấn Đề & Ranh Giới Khái Quát Hóa (Generalization Boundary)

### 1.1. Phân định vai trò tập dữ liệu (Dataset Role Separation)
- **Tập Held-out 60 ca cũ (`evaluation/v3/`):** Do đã được sử dụng nhiều lần để phân tích lỗi, tinh chỉnh prompt (`repair-prompt`, `safe-repair`), sửa schema và gỡ lỗi runtime trong suốt các giai đoạn Package 7 và Package 8, tập held-out cũ **không còn đủ tư cách làm tập xác nhận độc lập (unseen validation set)**. Nó được tái phân loại chính thức thành **Bộ kiểm thử hồi quy và phát triển (Development / Regression Benchmark Suite)**.
- **Bộ xác nhận độc lập mới (`locked-validation-v1`):** Được trích xuất từ 1.577 sản phẩm chưa từng xuất hiện trong tập phát triển 200 sản phẩm ban đầu, rà soát trùng lặp, giải quyết mâu thuẫn tác giả, và khóa bất biến (tamper-evident hash lock).

### 1.2. Thừa nhận trung thực về nguồn dữ liệu & miền bài toán (Domain Generalization Limits)
- Toàn bộ dữ liệu ứng viên vẫn thuộc **cùng họ nguồn Tiki Books v4 (Kaggle CC0-1.0)**.
- **Ranh giới khoa học:** Bộ dữ liệu này chứng minh khả năng khái quát hóa sang các **sản phẩm mới chưa từng thấy (unseen products/work groups)** trong cùng phân phối catalog Tiki. Nó **CHƯA chứng minh khả năng khái quát hóa sang nguồn dữ liệu khác (Cross-Source Generalization)** (ví dụ: Amazon Books, Google Books) hoặc các miền thương mại điện tử phi sách (ví dụ: Điện máy, Thời trang) do cấu trúc metadata và hành vi trích dẫn có sự khác biệt.

---

## 2. Kết Quả Rà Soát Trùng Lặp & Phụ Thuộc Tác Giả (Audit & De-duplication)

Dựa trên kết quả đối soát cấu trúc ([`structural-audit.json`](catalog-pair-candidate-v1/structural-audit.json)) và đối soát phụ thuộc tác giả ([`author-overlap-audit.json`](catalog-pair-candidate-v1/author-overlap-audit.json)):

1. **Trùng lặp Product ID:** $0 / 1.256$ ID trùng với tập phát triển.
2. **Loại bỏ xung đột nhan đề đa tác giả (Ambiguous Title Collisions):**
   - Loại bỏ 4 tác vụ liên quan đến 2 nhan đề xung đột:
     - *Sức Mạnh Của Ngôn Từ* (Shin Dohyeon vs. Don Gabor)
     - *Chuyện Con Ốc Sên Muốn Biết Tại Sao Nó Chậm Chạp* (Luis Sepulveda vs. Neil Gaiman)
3. **Phụ thuộc tác giả (Author Dependence & Clustering):**
   - 160 nhãn tác giả xuất hiện trên nhiều tác vụ khác nhau.
   - 422/628 tác vụ ban đầu có liên kết tác giả chung. Thành phần liên thông lớn nhất chứa 202 tác vụ (~32.2%).
   - **Giải pháp xử lý thống kê:** Thay vì giả định ngây thơ các câu hỏi độc lập $i.i.d.$, hệ thống gom 624 tác vụ sạch vào **250 cụm tác giả độc lập (250 Independent Author Clusters)**. Mọi kiểm định thống kê và tính khoảng tin cậy CI 95% phải sử dụng **đơn vị phân tích là cụm tác giả (Cluster-Robust Analysis Unit)** để tránh hiện tượng phóng đại giả tạo độ mạnh thống kê (artificial sample size inflation).

---

## 3. Quy Trình Oracle Độc Lập (Independent Oracle Verification Protocol)

Để đảm bảo ground-truth không bị thiên lệch bởi pipeline SUT:
1. **Trích xuất thuộc tính độc lập:** Giá niêm yết, điểm đánh giá trung bình, và số trang được trích xuất trực tiếp từ các trường gốc trong `catalog-products.jsonl` (bản snapshot đã băm SHA-256).
2. **Khóa nhãn nhị phân:** Quyết định so sánh (ví dụ: Sách A rẻ hơn Sách B bao nhiêu đồng, Sách nào có rating cao hơn) được tính toán tiền định bằng logic số học độc lập.
3. **Tiêu chí kiểm định oracle:**
   - Giá bán phải là số nguyên dương hợp lệ (`price_vnd > 0`).
   - Điểm đánh giá nằm trong khoảng năm sao hợp lệ ($1.0 \le \text{rating} \le 5.0$).
   - Nhan đề và tác giả được chuẩn hóa NFKD loại bỏ các cụm từ ấn bản/quà tặng để tránh sai lệch tra cứu.

---

## 4. Bảng Kê Băm Bất Biến (Locked Manifest Digests)

Tập dữ liệu xác nhận đã được đóng băng tại: [`evaluation/v3/thesis-validation-v1/locked-validation-v1/`](locked-validation-v1/)

| File Dữ Liệu | Số Lượng Bản Ghi | Mã Băm SHA-256 |
| :--- | :---: | :--- |
| [`locked-prompts.jsonl`](locked-validation-v1/locked-prompts.jsonl) | 624 prompts | Xem trong `locked-validation-manifest.json` |
| [`locked-answer-key.jsonl`](locked-validation-v1/locked-answer-key.jsonl) | 624 oracles | Xem trong `locked-validation-manifest.json` |
| [`author-cluster-mapping.json`](locked-validation-v1/author-cluster-mapping.json) | 250 clusters | Xem trong `locked-validation-manifest.json` |
| [`locked-validation-manifest.json`](locked-validation-v1/locked-validation-manifest.json) | 1 manifest | Băm toàn vẹn hệ thống |
