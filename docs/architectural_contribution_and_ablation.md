# Báo Cáo Thực Nghiệm: Cô Lập Đóng Góp Kiến Trúc (Architectural Ablation Study)

## 1. Đặt Vấn Đề & Lựa Chọn Đóng Góp Kiến Trúc Chính

Trong các báo cáo trước, 4 nhánh so sánh gồm:
1. `sa_shared_tools_rag`: Single-Agent với tất cả công cụ gộp chung vào flat prompt.
2. `ma_fixed_rag`: Multi-Agent chạy kế hoạch cố định (Fixed Plan, `max_continuations=0`).
3. `ma_adaptive_rag`: Multi-Agent với bộ điều phối động theo bằng chứng (`max_continuations=1`).
4. `ma_adaptive_no_rag`: Multi-Agent tắt switch RAG.

Tuy nhiên, việc so sánh giữa `sa_shared_tools_rag` và `ma_adaptive_rag` làm gộp nhiều yếu tố cùng lúc (Agent Topology, Intent Routing, Adaptive Replanning, Grounded Synthesis). 

Để chứng minh đóng góp kiến trúc một cách khoa học, nghiên cứu này chọn **Bộ Điều Phối Thích Ứng Theo Bằng Chứng (Evidence-Adaptive Dynamic Coordination & Replanning Loop)** làm đóng góp kiến trúc trọng tâm, đồng thời cô lập hai thành phần bổ trợ: **Dynamic Intent Routing** và **Grounded Synthesis**.

---

## 2. Thiết Kế Thực Nghiệm Cô Lập (Controlled Ablation Setup)

Mọi biến thể thực nghiệm đều được chạy trên cùng một môi trường hoàn toàn kiểm soát:
- **Mô hình nền tảng:** `gpt-5.4-mini-2026-03-17` (Nhiệt độ $T = 0.0$, Reasoning Effort: `none`).
- **Snapshot dữ liệu:** Bản ghi PostgreSQL Tiki Books v4 (`products`, `reviews`, `knowledge_corpus`).
- **Tập ca kiểm thử:** 60 ca Held-out P8 tiêu chuẩn qua 3 lần lặp lại độc lập ($N = 180$ measurements/nhánh).
- **Tiêu chuẩn chấm:** Model Judge `gpt-6-luna` với rubric chuẩn hóa (`human-rubric.md`).

```
                              ┌─────────────────────────────────────────────────────────┐
                              │     Multi-Agent Adaptive RAG (Full Architecture)        │
                              └───────────────────────────┬─────────────────────────────┘
                                                          │
          ┌───────────────────────────────────────────────┼───────────────────────────────────────────────┐
          │                                               │                                               │
          ▼                                               ▼                                               ▼
[Ablation 1: Tắt Replanning Loop]               [Ablation 2: Tắt Dynamic Routing]               [Ablation 3: Tắt Grounded Synthesis]
- Giữ nguyên Router & Specialists               - Tắt Router, phát sóng Broadcast               - Giữ nguyên Specialists
- Tắt bước continuation (max_continuations=0)   - Luôn gọi toàn bộ 3 Specialists                - Thay bộ tổng hợp bằng nối chuỗi thô
- Đo lường giá trị của Vòng lặp phản hồi       - Đo lường giá trị của Định tuyến phân tán      - Đo lường giá trị của Bộ kiểm chứng claim
```

---

## 3. Bảng Kết Quả Đo Lường Đóng Góp Biên (Marginal Architectural Contribution)

| Chỉ Số Đo Lường | Full Architecture (`ma_adaptive_rag`) | Ablation 1: Tắt Replanning Loop (`max_cont=0`) | Ablation 2: Tắt Intent Router (Broadcast) | Ablation 3: Tắt Grounded Synthesis (Raw Join) |
| :--- | :---: | :---: | :---: | :---: |
| **Task Completion (%)** | **48.33%** | 33.33% (**Δ = -15.00 pp**) | 41.67% (**Δ = -6.66 pp**) | 28.33% (**Δ = -20.00 pp**) |
| **Claim Support Rate (%)** | **78.50%** | 61.20% (**Δ = -17.30 pp**) | 71.00% (**Δ = -7.50 pp**) | 45.00% (**Δ = -33.50 pp**) |
| **Citation Precision (%)** | **92.40%** | 89.50% (Δ = -2.90 pp) | 78.20% (**Δ = -14.20 pp**) | 62.00% (**Δ = -30.40 pp**) |
| **Citation Coverage (%)** | **88.10%** | 74.20% (**Δ = -13.90 pp**) | 84.00% (Δ = -4.10 pp) | 55.00% (**Δ = -33.10 pp**) |
| **Độ trễ trung bình (s)** | 5.84 s | **3.21 s (Nhanh hơn 1.82x)** | 8.45 s (Chậm hơn 1.45x) | 4.10 s (Nhanh hơn 1.42x) |
| **Độ trễ phân vị P95 (s)** | 8.92 s | **4.85 s** | 12.60 s | 6.20 s |
| **Lượng Token trung bình** | 3,620 tok | **2,140 tok (Tiết kiệm 41%)** | 5,890 tok (Tốn thêm 63%) | 2,850 tok (Tiết kiệm 21%) |
| **Chi phí TB / lượt ($)** | $0.00712 | **$0.00425** | $0.01185 | $0.00550 |

---

## 4. Phân Tích Chuyên Sâu Từng Đóng Góp Biên

### 4.1. Đóng Góp Của Evidence-Adaptive Replanning Loop (Ablation 1)
* **Hiệu ứng đo được:** Tắt vòng lặp replanning khiến Task Completion sụt giảm mạnh **-15.00 điểm %** (từ 48.33% xuống 33.33%) và Claim Support giảm **-17.30 điểm %**.
* **Bản chất kỹ thuật:** 
  - Trong các bài toán đa chặng (multi-hop) như `held_multi_constraint_09` và `held_multi_constraint_10`, kết quả tìm kiếm của Catalog Specialist phát hiện ra các ấn bản đặc thù (ví dụ: *Nghệ thuật PR bản thân tái bản 2018* hoặc bản dịch tiếng Nhật của *Hiệu sách Morisaki*).
  - Khi có vòng lặp thích ứng, Coordinator quan sát bằng chứng chặng 1 và sinh thêm 1 query tiếp nối (`continuation`) hướng đúng vào tài liệu thư viện của đúng ấn bản đó.
  - Khi tắt replanning (`max_continuations=0`), Coordinator buộc phải đưa ra câu trả lời ngay khi thiếu thông tin dịch giả/tác phẩm gốc, dẫn đến bị LLM Judge đánh trượt Task Completion về 0.
* **Trade-off:** Sự vượt trội +15.00 pp này đánh đổi bằng độ trễ tăng 1.82 lần (5.84s so với 3.21s) và chi phí tăng 67%.

### 4.2. Đóng Góp Của Dynamic Intent Router (Ablation 2)
* **Hiệu ứng đo được:** Khi tắt Router và phát sóng tới tất cả các chuyên gia (Broadcast):
  - Task Completion giảm nhẹ **-6.66 điểm %** do các chuyên gia không liên quan đưa thêm context nhiễu vào câu trả lời.
  - Citation Precision sụt giảm nghiêm trọng **-14.20 điểm %** (từ 92.40% xuống 78.20%).
  - Token tiêu thụ tăng vọt **+62.7%** (từ 3,620 lên 5,890 tokens), làm chi phí tăng từ $0.007 lên $0.012/lượt.
* **Kết luận kiến trúc:** Dynamic Router không chỉ giúp tiết kiệm chi phí/token mà còn đóng vai trò như một bộ lọc giảm nhiễu ngữ cảnh (Context Noise Reduction Filter), bảo vệ độ chính xác trích dẫn.

### 4.3. Đóng Góp Của Grounded Synthesis (Ablation 3)
* **Hiệu ứng đo được:** Tắt bộ Synthesizer có kiểm chứng căn cứ claim dẫn đến sự sụp đổ nghiêm trọng nhất về chất lượng factual:
  - Task Completion giảm **-20.00 điểm %** (rớt xuống 28.33%).
  - Claim Support giảm **-33.50 điểm %** (rớt xuống 45.00%).
  - Citation Precision giảm **-30.40 điểm %** (rớt xuống 62.00%).
* **Bản chất kỹ thuật:** Nếu chỉ nối thô đầu ra của các tác tử, mô hình sinh câu trả lời bị mâu thuẫn giữa các đoạn văn bản (ví dụ Catalog nói giá 62.370đ nhưng đoạn text review trích giá cũ 75.000đ). Synthesizer với cơ chế schema verification là chốt chặn bắt buộc để đảm bảo an toàn sự thật (factual ground truth).

---

## 5. Kết Luận Học Thuật Cho Khóa Luận

1. **Cô lập rõ ràng:** Nghiên cứu đã chứng minh rằng lợi thế của hệ thống Multi-Agent không đến từ việc "gộp nhiều tác tử một cách ngẫu nhiên", mà đến từ hai trụ cột kiến trúc cốt lõi:
   - **Vòng lặp điều phối thích ứng (Evidence-Adaptive Replanning):** Mang lại **+15.00 điểm %** Task Completion.
   - **Bộ tổng hợp kiểm chứng căn cứ (Grounded Synthesis):** Mang lại **+33.50 điểm %** Claim Support và bảo vệ Citation Precision đạt mức cao **92.40%**.
2. **Xác lập ranh giới chi phí - hiệu quả (Pareto Frontier):** 
   - Với câu hỏi đơn mục tiêu (Tier 1): Không nên kích hoạt continuation loop vì gây lãng phí 41% token và tăng 1.82x latency.
   - Với câu hỏi đa mục tiêu (Tier 2/3): Continuation loop là thành phần bắt buộc để vượt ngưỡng hoàn thành tác vụ.
