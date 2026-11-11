# Implementation Plan: Academic Thesis Report Completion (6-Chapter Standard)

- **Date:** 2026-11-11
- **Target Repository:** `latex/`
- **Output Artifact:** `latex/build/main.pdf` (and `latex/bao-cao-thanh-v4.pdf`)
- **Engine:** Tectonic 0.17.0
- **Scope:** Complete expansion from 21 pages to comprehensive ~35–45 page academic report with rigorous data, theory, architecture, evaluation (highlighting RAG ablation), remediation, and appendices.

---

## 1. Chapter Decomposition and Responsibilities

1. **`latex/01-tong-quan.tex` (Chương 1: Tổng quan và Cơ sở Lý thuyết)**
   - Bối cảnh thương mại điện tử sách tiếng Việt và các thách thức cốt lõi: Ảo giác (Hallucination), trích dẫn sai nguồn, dữ liệu mâu thuẫn giữa thông tin bán hàng và đánh giá người mua.
   - Mục tiêu nghiên cứu và giới hạn phạm vi (historical snapshot vs live market).
   - Cơ sở lý thuyết và khảo cứu công trình liên quan (Related Work):
     - Single-Agent vs Multi-Agent Systems (MAS): AutoGen, CrewAI, LangGraph.
     - Phân định rõ ràng: Autonomous Agents vs Bounded Multi-Expert Orchestration.
     - Cơ chế Retrieval-Augmented Generation (RAG) và bài toán Factual Grounding.

2. **`latex/02-du-lieu.tex` (Chương 2: Kỹ thuật Dữ liệu và Kho Tri thức)**
   - Nguồn gốc dữ liệu: Kaggle Tiki Books v4 (snapshot thu thập ngày 2026-08-24).
   - Pipeline tiền xử lý: Loại trùng lặp, chuẩn hóa schema, khử định danh thông tin nhạy cảm (PII redaction: số điện thoại, email, URL).
   - Các profiles dữ liệu: `test` (24 sách / 115 reviews), `eval` (200 records / 1.773 reviews / 199 valid books), `full` (local only).
   - Kho kiến thức tuyển chọn và hiệu chuẩn (`books-v1-calibrated-20260909`): 20 nguồn/chunks, vector embedding `text-embedding-3-small` (1536 chiều), phân loại mức độ đối chiếu (tác phẩm chính xác, mơ hồ, chưa khớp).
   - Nguyên tắc bất biến và giới hạn diễn giải: Snapshot lịch sử không tạo giả định thị trường.

3. **`latex/03-kien-truc.tex` (Chương 3: Thiết kế Kiến trúc và Cơ chế V2)**
   - Kiến trúc tổng thể Modular Monolith và Agent Gateway (kiểm soát quyền truy cập, fail-closed, zero violation).
   - Thiết kế V2 Durable Services trên PostgreSQL (conversations, turns, actions, durable state, SQL lease lock).
   - Bounded Hybrid Multi-Expert Supervisor:
     - 4 Domain Experts (`product`, `review`, `trust`, `market`) với trách nhiệm phân định rõ.
     - Quy trình lập kế hoạch có chặn trên (`MAX_PLAN_REVISIONS = 1`, `MAX_CANDIDATES = 5`, `MAX_EXPERT_STEPS = 8`).
   - Kiểm chứng căn cứ & Bảo toàn sự thật: Exact-span citation matching, automated semantic verification, Display Citation `[C1]`, `[C2]`.
   - Quản lý ngân sách và tài nguyên: `ProviderBudgetScope` với độ phân giải `nano_usd`, retry/circuit breaker.
   - Giao thức Server-Sent Events (SSE) với correlation ID, heartbeat và terminal event.

4. **`latex/04-thuc-nghiem.tex` (Chương 4: Thực nghiệm và Đánh giá Chuyên sâu)**
   - Thiết kế thực nghiệm và 4 biến thể so sánh: SA, MA Fixed, MA Adaptive, MA Adaptive no RAG.
   - Đánh giá thăm dò bộ v4 (24 câu hỏi, 144 lượt chạy): Phân tích hiệu ứng bão hòa trần (ceiling effect) và chênh lệch duy nhất ở câu *Sapiens (Tái Bản 2022)* do prompt bias.
   - Đánh giá trên bộ P8 Held-out chuẩn (60 hội thoại × 4 biến thể × 3 lượt = 720 lượt): SA 45.2% vs MA Adaptive 41.4% (chênh lệch -3.8%, CI 95% [-8.7%; +1.0%], p > 0.05), đánh đổi 4.73x thời gian và 2.78x token.
   - Lần chạy xác nhận (Confirmatory Run với Preregistration Protocol): Hiện tượng floor effect trên câu hỏi tự nhiên ngoài khuôn.
   - **Tâm điểm: Nghiên cứu Triệt tiêu (Ablation Study) & Đóng góp sống còn của RAG**:
     - Cú sập -36.1% Task Completion khi tắt RAG (từ 41.4% rớt xuống 5.2%).
     - Cú rơi -16.24% trong đợt chạy xác nhận (thua trắng 13/26 nhóm).
     - Khả năng từ chối đúng lúc (Refusal Calibration) giảm 74.4%.
     - Kết luận khoa học: RAG và Grounded Synthesis mới là nhân tố quyết định chất lượng, không phải Multi-Agent Topology.
   - Đánh giá độ tin cậy của bộ chấm tự động LLM Judge Luna (độ đồng thuận κ = 0.946 qua 2 lần chấm).

5. **`latex/05-cai-tien.tex` (Chương 5: Quá trình Rà soát, Khắc phục và Kiểm chứng)**
   - Lịch sử khắc phục P0, P1, P2:
     - P0: Quy trình báo cáo bất biến, không ghi đè dữ liệu.
     - P1: Đối soát checksum, phát hiện và loại bỏ số liệu mô phỏng / nhãn người viết sẵn, sửa migration.
     - P2: Cải tiến retrieval (giữ truy vấn gốc, cấp quyền tiêu đề khi chấm lexical, attribution giữa các nguồn).
   - Kiểm tra toàn vẹn bằng chứng (Audit `evidence_integrity` SHA-256).
   - Bộ kiểm thử tự động 1.456 tests hồi quy không gọi mô hình thật.

6. **`latex/06-ket-luan.tex` (Chương 6: Kết luận và Hướng phát triển)**
   - Tóm lược đóng góp thực tế.
   - Giới hạn hệ thống (quy mô 200 sách, độ phụ thuộc snapshot, chưa có human annotation).
   - Hướng phát triển: Mở rộng catalog quy mô lớn, thử nghiệm với người dùng thật, tối ưu độ trễ.

7. **`latex/07-phu-luc.tex` (Phụ lục)**
   - Phụ lục A: Chi tiết 24 ca thử nghiệm v4 và 43 facts chuẩn.
   - Phụ lục B: Prompt chuyên gia mẫu và Rubric chấm của Judge Luna.
   - Phụ lục C: Bảng thống kê chi tiết kết quả 720 lượt P8.
   - Phụ lục D: Tài liệu tham khảo (`sources.tex`).

---

## 2. Execution Steps

1. **Step 1:** Tạo và cập nhật `latex/main.tex` để import đúng 6 chương và phụ lục, bổ sung danh mục hình vẽ và danh mục bảng biểu.
2. **Step 2:** Viết chi tiết `latex/01-tong-quan.tex`.
3. **Step 3:** Viết chi tiết `latex/02-du-lieu.tex`.
4. **Step 4:** Viết chi tiết `latex/03-kien-truc.tex`.
5. **Step 5:** Viết chi tiết `latex/04-thuc-nghiem.tex`.
6. **Step 6:** Viết chi tiết `latex/05-cai-tien.tex`.
7. **Step 7:** Viết chi tiết `latex/06-ket-luan.tex`.
8. **Step 8:** Viết chi tiết `latex/07-phu-luc.tex`.
9. **Step 9:** Chạy lệnh biên dịch Tectonic qua `latex/Build.ps1`, đối soát log lỗi, kiểm tra số trang và xuất bản `bao-cao-thanh-v4.pdf`.
