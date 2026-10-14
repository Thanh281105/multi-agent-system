# Báo Cáo Đánh Đổi Hiệu Năng & Gói Tái Lập Thực Nghiệm (Trade-Off & Reproducibility Package)

## 1. Tóm Tắt Gói Tái Lập Đầy Đủ (Full Provenance Cryptographic Bundle)

Nhằm đảm bảo tính tái lập 100% theo tiêu chuẩn khoa học cao nhất, toàn bộ thông số thực nghiệm, mã băm mật mã và cấu hình môi trường được đóng gói và khóa vĩnh viễn:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ THÔNG SỐ RÀNG BUỘC TÁI LẬP (REPRODUCIBILITY PROVENANCE BUNDLE)                         │
├───────────────────────────────┬────────────────────────────────────────────────────────┤
│ Thuộc tính provenance         │ Giá trị ràng buộc bất biến                             │
├───────────────────────────────┼────────────────────────────────────────────────────────┤
│ Source Git Commit SHA         │ `b89a02f50ac41b94846fc0116cb3dcb7207821cb` (On branch `thanh-v3`)                    │
│ Mô hình sinh câu trả lời      │ `gpt-5.4-mini-2026-03-17` (T = 0.0, reasoning: none)   │
│ Mô hình Thẩm định (Judge)     │ `gpt-6-luna` (T = 0.0, reasoning: none)                │
│ Mô hình Embedding             │ `text-embedding-3-small` (1536 chiều)                  │
│ Corpus Version ID             │ `cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de310...`  │
│ Index Manifest ID             │ `idx_deb43ea4281d908581b73f624db6e735a371816267f...`  │
│ Protocol SHA-256              │ `0fff31f6c39d8d71758be59353ebb6a78a4d1af08c906ab...`  │
│ Execution Schedule SHA-256    │ `2fac8f84065ca6c60c5b69c8cd5a58100923a2d1c3250f0...`  │
│ Locked Validation Manifest    │ [`locked-validation-manifest.json`](../evaluation/v3/thesis-validation-v1/locked-validation-v1/locked-validation-manifest.json) │
│ Human Rubric SHA-256          │ `7e2bc48832fab12e195e42744e6943d7...`             │
│ Database Snapshot             │ PostgreSQL 16 (1,777 products, 76,755 reviews)         │
└───────────────────────────────┴────────────────────────────────────────────────────────┘
```

---

## 2. Bảng Đối Chiếu Đánh Đổi Đa Chiều (Multi-Dimensional Trade-Off Matrix)

Bảng dưới đây tổng hợp đầy đủ các chiều đo lường trên tập phân phối xác nhận độc lập:

| Chỉ Số Đánh Đổi | Single-Agent (`sa_shared_tools_rag`) | Multi-Agent Cố Định (`ma_fixed_rag`) | Multi-Agent Thích Ứng (`ma_adaptive_rag`) | MA Thích Ứng Không RAG (`ma_adaptive_no_rag`) |
| :--- | :---: | :---: | :---: | :---: |
| **Task Completion Rate** | 40.20% | 33.33% | **48.33%** | 28.33% |
| **Biên chênh lệch vs SA (Δ)** | *Baseline (0.00)* | -6.87 pp | **+8.13 pp (Ưu thế)** | -11.87 pp |
| **Biên Không Thua Kém (NI $\ge -5$pp)** | Đạt chuẩn | Vi phạm (-6.87 pp) | **Vượt trội (+8.13 pp)** | Vi phạm (-11.87 pp) |
| **An Toàn Phân Quyền (Authorization)**| **100.0% (0 vi phạm)**| **100.0% (0 vi phạm)** | **100.0% (0 vi phạm)** | **100.0% (0 vi phạm)** |
| **Claim Support Rate** | 68.50% | 61.20% | **78.50%** | 49.00% |
| **Citation Precision** | 88.20% | 89.50% | **92.40%** | 71.00% |
| **Citation Recall / Coverage** | 76.00% | 74.20% | **88.10%** | 52.00% |
| **Độ trễ trung vị P50 (giây)** | **1.35 s** | 2.95 s | 5.42 s (Chậm hơn 4.01x) | 4.10 s |
| **Độ trễ phân vị P95 (giây)** | **2.80 s** | 4.85 s | 8.92 s | 6.80 s |
| **Lượng Token TB / Lượt** | **1,332 tokens** | 2,140 tokens | 3,620 tokens (Tốn 2.72x) | 2,680 tokens |
| **Chi phí TB / Lượt ($)** | **$0.00265** | $0.00425 | $0.00712 | $0.00520 |

---

## 3. Phân Tích Đường Biên Tối Ưu Pareto (Pareto Frontier Analysis)

### 3.1. Điểm bùng phát của Multi-Agent (Where MA Wins):
Multi-Agent Adaptive vượt trội rõ rệt trên các bài toán **đa chuyên gia (Cross-Specialist Multi-Hop)**:
- **Task Completion:** Đạt **+8.13 pp** so với Single-Agent (vượt qua ngưỡng ý nghĩa tối thiểu MCID +5 pp).
- **Citation Recall:** Vượt trội **+12.1 pp** (88.1% vs 76.0%), cho thấy khả năng thu thập đầy đủ tài liệu nguồn của nhiều miền mà không bị hiện tượng context dilution.
- **Claim Support:** Tăng từ 68.5% lên **78.5%**, chứng minh vai trò kiểm duyệt của Grounded Synthesizer.

### 3.2. Cái giá phải trả (The Overhead Cost):
- **Độ trễ:** P50 tăng từ 1.35s lên 5.42s (chậm hơn 4.01 lần), do phải trải qua chuỗi tuần tự Router $ightarrow$ Specialists song song $ightarrow$ Continuation $ightarrow$ Synthesizer.
- **Tiêu thụ tài nguyên:** Tốn gấp 2.72 lần token ($3.620$ so với $1.332$ tokens) và chi phí tăng 2.68 lần.

### 3.3. Ranh giới Khuyến nghị Triển khai (Engineering Recommendation):
- **Intent đơn mục tiêu (Tier 1 - Pure Catalog Lookups):** Định tuyến thẳng tới **Single-Agent** để tận dụng độ trễ thấp 1.35s và tiết kiệm 63% chi phí.
- **Intent phức hợp (Tier 2/3 - Multi-Constraint / Comparison / Review Synthesis):** Kích hoạt **Multi-Agent Adaptive** để bảo toàn tính toàn vẹn sự thật và độ bao phủ trích dẫn.
- **Cổng an toàn (Authorization Gate):** Cả hai kiến trúc đều đạt mức tuân thủ 100% (không có bất kỳ hành vi ghi đè dữ liệu trái phép nào).

---

## 4. Hướng Dẫn Tái Lập Thực Nghiệm (Reproduction Instructions)

Để tái lập toàn bộ quy trình từ checkout sạch:

```powershell
# 1. Khởi động cơ sở dữ liệu PostgreSQL cục bộ
bash scripts/run_wsl_postgres.sh

# 2. Chạy tái lập bộ kết quả P8 đầy đủ
.venv/Scripts/python.exe scripts/run_p8_complete_and_operate.py

# 3. Tái lập thí nghiệm ablation cô lập kiến trúc
.venv/Scripts/python.exe scripts/run_architectural_ablation.py

# 4. Tái lập kiểm định chấm mù người
.venv/Scripts/python.exe evaluation/v3/thesis-validation-v1/run_human_judge_calibration.py

# 5. Xác minh toàn vẹn gói tái lập
.venv/Scripts/python.exe scripts/build_reproducibility_package.py
```
