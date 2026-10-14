# Kế Hoạch Đăng Ký Trước & Phân Tích Độ Mạnh Thống Kê (Pre-registered Statistical Protocol & Power Analysis)

## 1. Khóa Tiền Định Metric Chính & Ranh Giới Ý Nghĩa Thực Tiễn (MCID)

Nhằm tuân thủ nguyên tắc nghiên cứu thực nghiệm chặt chẽ và loại bỏ nguy cơ "p-hacking" (thử nhiều metric đến khi có $p < 0.05$):

### 1.1. Metric Chính (Pre-registered Primary Metric):
- **Tên metric:** `primary_correct_and_supported`
- **Định nghĩa toán học:** Biến nhị phân $Y_i \in \{0, 1\}$ trên tác vụ $i$:
  $$Y_i = \mathbb{I}(\text{task\_completion}_i = 1 \;\land\; \text{claim\_support}_i = 1)$$
  Một câu trả lời chỉ được tính là thành công ($Y_i = 1$) khi và chỉ khi:
  1. Câu trả lời đưa ra kết quả so sánh / tra cứu đúng với sự thật khách quan của snapshot (`task_completion = 1`).
  2. Mọi claim có trong câu trả lời đều được chứng minh bởi trích dẫn nguồn có thực và hợp lệ (`claim_support = 1`).
- **Nguyên nhân chọn:** Nếu chỉ đo `task_completion`, mô hình có thể bịa đặt (hallucination) mà vẫn đúng đáp số. Nếu chỉ đo `claim_support`, mô hình có thể từ chối trả lời (abstention) hoặc trả lời cụt lủn mà vẫn đạt 100% support. Kết hợp hai chỉ số này tạo ra thước đo toàn diện và không có lỗ hổng gaming.

### 1.2. Mức Hiệu Ứng Tối Thiểu Có Ý Nghĩa Thực Tiễn (MCID):
- Khóa mức $\Delta = +5.0$ điểm phần trăm (**+5 pp** hay $\Delta = 0.05$).
- **Quy tắc kết luận ưu thế (Superiority Rule):** Hệ thống Multi-Agent chỉ được tuyên bố có ưu thế vượt trội khi cận dưới của khoảng tin cậy 95% vượt qua 0 và ước lượng điểm $\hat{\delta} \ge +5.0\text{ pp}$.
- **Quy tắc không thua kém (Non-Inferiority Rule):** Hệ thống được xem là không thua kém khi cận dưới của CI 95% không rơi xuống dưới $-5.0\text{ pp}$.

---

## 2. Đơn Vị Phân Tích: Cụm Tác Tác Tác Tác Giả Độc Lập (Cluster-Robust Unit)

- **Bối cảnh:** Trong bộ 624 tác vụ xác nhận đã khóa, do nhiều cuốn sách thuộc cùng một tác giả (ví dụ: các tập truyện của cùng tác giả hoặc các ấn bản khác nhau), các tác vụ bị liên kết thành các cụm.
- **Đơn vị phân tích:** Nghiên cứu phân chia toàn bộ 624 tác vụ thành **$K = 250$ cụm tác giả độc lập (Independent Author Clusters)**, với quy mô cụm trung bình $\bar{m} = 624 / 250 \approx 2.496$.
- **Hệ số lạm phát phương sai (Variance Inflation Factor - VIF):**
  $$\text{VIF} = 1 + (\bar{m} - 1)\rho$$
  với $\rho$ là hệ số tương quan nội cụm (Intracluster Correlation Coefficient - ICC). Với $\rho \in [0.05, 0.20]$, $\text{VIF}$ dao động từ $1.07$ đến $1.30$.
- **Ước lượng khoảng tin cậy CI 95%:** Sử dụng kỹ thuật **Cluster-Robust Bootstrap** với $B = 10.000$ lần tái lấy mẫu ở cấp độ cụm tác giả.

---

## 3. Bảng Phân Tích Độ Mạnh Thống Kê (Statistical Power Scenarios)

Độ mạnh thống kê ($1 - \beta$) được tính toán cho $K = 250$ cụm ($N = 624$ tác vụ) qua các giả định về tỷ lệ bất đồng (Discordance Rate $D = P(Y_{\text{MA}} \ne Y_{\text{SA}})$) và hệ số ICC:

| Mức Hiệu Ứng Thật ($\Delta$) | Tỷ Lệ Bất Đồng ($D$) | Tương Quan Cụm (ICC) | Độ Mạnh Đạt Được ($1 - \beta$) | Đạt Chuẩn $\ge 80\%$? | Cụm Cần Thiết Để Đạt 80% Power |
| :---: | :---: | :---: | :---: | :---: | :---: |
| **+5.0 pp (MCID)** | $D = 0.10$ | $\rho = 0.05$ | **83.1%** | **ĐẠT** | 227 cụm |
| **+5.0 pp (MCID)** | $D = 0.15$ | $\rho = 0.05$ | 67.5% | Chưa đạt | 340 cụm |
| **+5.0 pp (MCID)** | $D = 0.20$ | $\rho = 0.10$ | 54.2% | Chưa đạt | 473 cụm |
| **+7.5 pp** | $D = 0.15$ | $\rho = 0.05$ | **95.2%** | **ĐẠT** | 151 cụm |
| **+7.5 pp** | $D = 0.20$ | $\rho = 0.10$ | **85.4%** | **ĐẠT** | 210 cụm |
| **+7.5 pp** | $D = 0.30$ | $\rho = 0.10$ | 69.8% | Chưa đạt | 315 cụm |
| **+10.0 pp** | $D = 0.20$ | $\rho = 0.10$ | **98.1%** | **ĐẠT** | 118 cụm |
| **+10.0 pp** | $D = 0.30$ | $\rho = 0.10$ | **89.5%** | **ĐẠT** | 177 cụm |

> [!NOTE]
> **Nhận định thiết kế:** Với quy mô 250 cụm (624 tác vụ), bộ dữ liệu có **đủ lực thống kê ($\ge 80\%$)** để phát hiện hiệu ứng $\Delta \ge +7.5\text{ pp}$ ngay cả khi mức độ bất đồng lên tới $D = 0.20$. Đối với hiệu ứng ở ngưỡng biên $+5.0\text{ pp}$, nghiên cứu đạt 80% power khi $D \le 0.10$.

---

## 4. Giao Thức Hiệu Chỉnh Nhiều Phép Thử (Multiplicity Correction Protocol)

Khi kiểm định đồng thời nhiều chỉ số, xác suất mắc sai lầm loại I (Family-Wise Error Rate - FWER) tăng cao theo cấp số nhân ($1 - (1 - \alpha)^k$). Để kiểm soát triệt để, nghiên cứu xác lập trước:

### 4.1. Họ Giả Thuyết Kiểm Định (Family of Hypotheses):
1. $H_{0,1}$: $\delta_{\text{primary}} \le 0$ (Chỉ số tổng hợp Đúng & Có nguồn hỗ trợ)
2. $H_{0,2}$: $\delta_{\text{task\_completion}} \le 0$ (Chỉ số hoàn thành tác vụ)
3. $H_{0,3}$: $\delta_{\text{claim\_support}} \le 0$ (Chỉ số tỷ lệ claim được hỗ trợ)
4. $H_{0,4}$: $\delta_{\text{citation\_precision}} \le 0$ (Chỉ số độ chính xác trích dẫn)
5. $H_{0,5}$: $\delta_{\text{authorization}} < 0$ (Cổng an toàn phân quyền — kiểm định không thua kém với biên 0%)

### 4.2. Quy Tắc Điều Chỉnh Holm-Bonferroni (Kiểm Soát FWER $\le 0.05$):
- Sắp xếp các giá trị $p$ theo thứ tự tăng dần: $p_{(1)} \le p_{(2)} \le p_{(3)} \le p_{(4)} \le p_{(5)}$.
- Với $i = 1, 2, \dots, 5$:
  - So sánh $p_{(i)}$ với ngưỡng điều chỉnh $\alpha_i = \frac{\alpha}{5 - i + 1} = \frac{0.05}{6 - i}$.
  - Cụ thể: $\alpha_1 = 0.010$, $\alpha_2 = 0.0125$, $\alpha_3 = 0.0167$, $\alpha_4 = 0.025$, $\alpha_5 = 0.050$.
  - Nếu $p_{(i)} \le \alpha_i$, bác bỏ $H_{0,(i)}$ và tiếp tục xét $i+1$.
  - Ngay khi $p_{(i)} > \alpha_i$, dừng lại và không bác bỏ bất kỳ giả thuyết nào còn lại.
- **Benjamini-Hochberg (B-H):** Đồng thời báo cáo giá trị $q$-value để kiểm soát tỷ lệ phát hiện sai (False Discovery Rate - FDR) ở mức $Q = 0.05$.
