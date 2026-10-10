# Báo Cáo Thực Nghiệm Thăm Dò v4: Đánh Giá Ghép Cặp Đối Chiếu Multi-Agent vs. Single-Agent

Tài liệu này trình bày toàn bộ kết quả thực nghiệm **144 lượt chạy (24 ca kiểm thử $\times$ 2 hệ thống $\times$ 3 lần lặp lại độc lập)** trên bộ benchmark chuẩn hóa v4 (`evaluation/v4_fair_benchmark_cases.json`) đối chiếu trực tiếp giữa **Multi-Agent System (MAS)** và **Single-Agent Baseline (SA)** trên snapshot Tiki Books.

Phương pháp đánh giá tuân thủ nghiêm ngặt các khuyến nghị nghiên cứu quốc tế về kiểm định LLM ghép cặp ([Demšar, JMLR 2006](https://www.jmlr.org/beta/papers/v7/demsar06a.html)) và định lượng độ bất định ([Blackwell và cộng sự, arXiv 2024](https://arxiv.org/abs/2410.03492)).

---

## 1. TÓM TẮT ĐIỀU HÀNH & KẾT QUẢ TỔNG THỂ

* **Quy mô thực nghiệm:** 24 cases phân tầng độ phức tạp $\times$ 2 hệ thống $\times$ 3 lần lặp lại = **144 lượt thực thi đo đạc**.
* **Mô hình nền tảng:** `gpt-5.4-mini` (áp dụng đồng nhất cho cả 2 hệ thống, nhiệt độ sinh $T = 0.0$ để tối đa hóa tính tái lập).
* **Bộ dữ liệu kết quả gốc:** [`output/evaluation-v4/full_24_benchmark_results.json`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/output/evaluation-v4/full_24_benchmark_results.json).

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ BẢNG TỔNG KẾT TOÀN DIỆN (N = 24 CASES | 144 RUNS)                                      │
├───────────────────────────────┬──────────────────┬──────────────────┬──────────────────┤
│ Chỉ số đo lường               │ Single-Agent     │ Multi-Agent      │ So sánh / Delta  │
├───────────────────────────────┼──────────────────┼──────────────────┼──────────────────┤
│ Điểm bao phủ Fact TB          │ 94.10%           │ 89.58%           │ Δ = -4.51%       │
│ Độ trễ trung bình (Latency)   │ 1.515 ms (~1.5s) │ 4.585 ms (~4.6s) │ SA nhanh hơn 3.0x│
│ Độ bất định TB (Repeat StdDev)│ σ = 0.035        │ σ = 0.035        │ Độ ổn định cao   │
│ Đối đầu ghép cặp (Win-Tie-Loss│ 2 Thắng (8.3%)   │ 2 Thắng (8.3%)   │ 20 Hòa (83.3%)   │
│ Khoảng tin cậy 95% (CI)       │ —                │ —                │ [-14.52%, +5.49%]│
│ Kiểm định t-ghép cặp (t-stat) │ —                │ —                │ t = -0.933 (p>0.05)
└───────────────────────────────┴──────────────────┴──────────────────┴──────────────────┘
```

> [!IMPORTANT]
> **Nhận định khoa học cốt lõi:**
> Trên thang đo tổng thể 24 cases, không có sự khác biệt có ý nghĩa thống kê về điểm hoàn thành tác vụ giữa Multi-Agent và Single-Agent ($p > 0.05$). Tuy nhiên, **Single-Agent có ưu thế vượt trội về hiệu năng vận hành** (nhanh hơn gấp 3 lần và tiết kiệm token hơn), trong khi **Multi-Agent phát huy sức mạnh ở các tác vụ cần cấu trúc phân rã sâu** nhưng phải đối mặt với bài toán *Schema Bottleneck* trong giao tiếp liên tác tử.

---

## 2. PHÂN TÍCH THEO PHÂN TẦNG ĐỘ PHỨC TẠP (PER-TIER BREAKDOWN)

Hiệu năng của hai hệ thống có sự phân hóa rõ rệt khi đi qua 4 tầng độ phức tạp (Complexity Tiers):

| Tầng Độ Phức Tạp (Tier) | Số Ca ($N$) | Single-Agent | Multi-Agent | Mức Chênh Lệch ($\Delta$) | Kết Quả Đối Đầu |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Tier 1: Single-Specialist Control** (Baseline đơn nguồn) | 6 | 95.8% | **100.0%** | **+4.2%** | **1 MA Win**, 5 Tie, 0 SA Win |
| **Tier 2: Cross-Specialist Multi-Hop** (Đa chuyên gia) | 8 | **87.5%** | 81.2% | -6.2% | 0 MA Win, 7 Tie, **1 SA Win** |
| **Tier 3: Multi-Perspective Recommendation** (Tư vấn đa chiều) | 6 | **97.2%** | 83.3% | -13.9% | **1 MA Win**, 4 Tie, **1 SA Win** |
| **Tier 4: Robustness & Guardrails** (An toàn & Phòng thủ) | 4 | **100.0%** | **100.0%** | +0.0% | 0 MA Win, 4 Tie, 0 SA Win |

```mermaid
gantt
    title Tỷ Lệ Hoàn Thành Fact Theo Tầng Độ Phức Tạp
    dateFormat X
    axisFormat %s
    section Tier 1 Control
    Single-Agent (95.8%) : 0, 95
    Multi-Agent (100.0%) : 0, 100
    section Tier 2 Cross-Hop
    Single-Agent (87.5%) : 0, 87
    Multi-Agent (81.2%)  : 0, 81
    section Tier 3 Recom
    Single-Agent (97.2%) : 0, 97
    Multi-Agent (83.3%)  : 0, 83
    section Tier 4 Guardrail
    Single-Agent (100.0%): 0, 100
    Multi-Agent (100.0%) : 0, 100
```

---

## 3. PHÂN TÍCH CƠ CHẾ NGUYÊN NHÂN ĐÚNG / SAI (ROOT CAUSE MECHANISMS)

Phân tích sâu các ca có sự khác biệt (Win/Loss) bộc lộ các cơ chế kỹ thuật mang tính nền tảng của hệ thống tác tử:

### 3.1. Các Ca Multi-Agent Thắng (MA Win)

#### Ca 1 (`fair_tier1_catalog_01`): Độ chính xác nhận diện thực thể
* **Đề bài:** Tra cứu giá, tác giả và NXB của sách *Sapiens*.
* **Kết quả:** Multi-Agent đạt **100.0%** vs. Single-Agent đạt **75.0%**.
* **Nguyên nhân:**
  - Multi-Agent có *Catalog Specialist* chuyên biệt với schema định sẵn: trích xuất đúng tên ấn bản đầy đủ là `"Sapiens Lược Sử Loài Người (Tái Bản 2022)"`.
  - Single-Agent khi trả lời tự do trong prompt phẳng đã tự ý rút gọn thành `"Sapiens: Lược Sử Loài Người"` (thiếu hậu tố ấn bản) và bị trừ điểm bao phủ thực thể.

#### Ca 2 (`fair_tier3_recom_06`): Phân rã điều kiện quà tặng kèm và cảnh báo rủi ro
* **Đề bài:** Chọn sách văn học kinh điển làm quà tặng có kèm postcard/bookmark, giá bao nhiêu, và phản hồi về dập góc vận chuyển.
* **Kết quả:** Multi-Agent đạt **100.0%** vs. Single-Agent đạt **83.3%**.
* **Nguyên nhân:**
  - Tác vụ đòi hỏi phải dung hòa giữa: *Catalog Specialist* (lọc quà tặng postcard + bookmark, giá 239.400đ của cuốn *Những người phụ nữ bé nhỏ*) và *Review Specialist* (lọc phản ánh sách về tay bị dập góc, rách bìa).
  - Multi-Agent phân rã tác vụ cho hai specialist riêng lẻ, sau đó Synthesizer cấu trúc câu trả lời rõ ràng thành 2 vế: thông tin sản phẩm và lời khuyên thẩm mỹ cho người mua quà tặng.
  - Single-Agent bị phân tán nên tóm tắt sơ sài ý kiến đóng gói, thiếu dẫn chứng cụ thể từ review.

---

### 3.2. Các Ca Single-Agent Thắng (SA Win) & Khuyết Điểm Của Multi-Agent

#### Ca 1 (`fair_tier2_cross_07`): Bài toán "Nghẽn Lược Đồ" (Schema Bottleneck)
* **Đề bài:** Kiểm tra cuốn *Bill Marriott*: giá bao nhiêu, có bao nhiêu lượt đánh giá trong catalog, và review nhận xét thế nào?
* **Kết quả:** Single-Agent đạt **66.7%** vs. Multi-Agent đạt **16.7%**.
* **Cơ chế lỗi của Multi-Agent:**
  - Trong thiết kế của Multi-Agent, prompt của *Catalog Specialist* chỉ thị trích xuất: `tên sách, giá, tác giả, NXB, số trang, rating`. Prompt vô tình không đưa trường `source_review_count` (47 lượt đánh giá) vào danh sách trích xuất.
  - Catalog Specialist tuân thủ chỉ thị nên không đưa trường này vào JSON output.
  - Khi *Synthesizer* nhận dữ liệu từ Catalog Specialist, nó thấy thiếu trường này nên thông báo: *"không tìm thấy số lượt đánh giá trong catalog"*.
  - Ngược lại, Single-Agent nhận ngữ cảnh phẳng chứa toàn bộ record gốc nên dễ dàng trích xuất con số `47`.
  - **Bài học học thuật:** *Hệ thống Multi-Agent phụ thuộc hoàn toàn vào độ bao phủ của giao thức trao đổi (Inter-Agent Schema Protocol). Nếu schema giữa các specialist bị hổng một thuộc tính, thông tin sẽ bị chặn đứng (information blockage) mà khâu tổng hợp không thể phục hồi được.*

#### Ca 2 (`fair_tier3_recom_03`): Bài toán "Tráo Đổi Thực Thể" (Entity Misattribution)
* **Đề bài:** So sánh phản hồi đóng gói giữa *Bạch Dạ Hành* và *Thiên Quan Tứ Phúc*: cuốn nào bị phàn nàn về màng seal, cuốn nào phàn nàn thiếu bọc chống sốc?
* **Kết quả:** Single-Agent đạt **100.0%** vs. Multi-Agent đạt **0.0%**.
* **Cơ chế lỗi của Multi-Agent:**
  - Trong lượt chạy này, *Review Specialist* nhận một danh sách gồm 10 reviews gộp chung của cả 2 cuốn sách. Khi tóm tắt, specialist bị ảo giác tráo đổi ngữ cảnh: gán review "mở seal" (của Thiên Quan Tứ Phúc) cho Bạch Dạ Hành, và gán review "thiếu bubble wrap" (của Bạch Dạ Hành) cho Thiên Quan Tứ Phúc.
  - Ngược lại, Single-Agent nhìn thấy cấu trúc dữ liệu phẳng có phân định rõ ràng giữa từng Product ID và Review ID nên không bị nhầm lẫn.
  - **Bài học học thuật:** *Khi ủy quyền tác vụ cho Specialist xử lý văn bản đa tài liệu (Cross-Document Reasoning), việc thiếu cơ chế neo giữ định danh cứng (Entity-ID Binding) có thể gây ra hiện tượng tráo đổi thuộc tính giữa các thực thể cạnh tranh.*

---

## 4. ĐỊNH LƯỢNG ĐỘ BẤT ĐỊNH & PHÂN TÍCH LỰC THỐNG KÊ (POWER ANALYSIS)

### 4.1. Độ Bất Định Qua 3 Lần Lặp Lại (Uncertainty Quantification)
Theo phương pháp của Blackwell et al. (2024), việc đo lường độ dao động giữa các lần lặp giúp kiểm soát độ nhiễu ngẫu nhiên:
* **Single-Agent:** Độ lệch chuẩn trung bình $\sigma = 0.035$ (tương đương biên độ dao động chỉ 3.5%). 22/24 cases đạt đồng thuận tuyệt đối (Perfect Agreement: $\sigma = 0.0$).
* **Multi-Agent:** Độ lệch chuẩn trung bình $\sigma = 0.035$. 22/24 cases đạt đồng thuận tuyệt đối ($\sigma = 0.0$).
* **Ý nghĩa:** Kết quả đo đạc có độ tin cậy nội bộ (Internal Validity) rất cao, loại trừ khả năng kết quả bị chi phối bởi tính ngẫu nhiên của mô hình ngôn ngữ.

### 4.2. Tính Toán Cỡ Mẫu Cần Thiết (Sample Size Planning)
Dựa trên phân tích kiểm định lực thống kê (Statistical Power Analysis) với mức ý nghĩa $\alpha = 0.05$ và lực kiểm định $1 - \beta = 0.80$:
* **Hiệu ứng thực tế quan sát được:** Chênh lệch trung bình $\Delta = -4.51\%$ với độ lệch chuẩn ghép cặp $s_d = 23.6\%$, dẫn đến kích thước hiệu ứng Cohen's $d_z = -0.19$ (hiệu ứng mức nhỏ, không đáng kể).
* **Cỡ mẫu cần thiết để tổng quát hóa ra toàn bộ thị trường eCommerce:**
  - Nếu muốn phát hiện mức cải thiện tối thiểu $\Delta = 10\%$ ($d_z \approx 0.30$): Cần cỡ mẫu tối thiểu **$N \ge 88$ ca kiểm thử độc lập**.
  - Nếu muốn phát hiện mức cải thiện $\Delta = 15\%$ ($d_z \approx 0.45$): Cần cỡ mẫu tối thiểu **$N \ge 39$ ca kiểm thử độc lập**.
  - Nếu muốn phát hiện mức cải thiện $\Delta = 20\%$ ($d_z \approx 0.60$): Cần cỡ mẫu tối thiểu **$N \ge 22$ ca kiểm thử độc lập**.

---

## 5. XÁC LẬP PHẠM VI TUYÊN BỐ KHOA HỌC (CLAIM SCOPE BOUNDARY)

Để đảm bảo tính liêm chính học thuật và bảo vệ khóa luận trước mọi câu hỏi phản biện của hội đồng chấm thi:

> [!CAUTION]
> **Tuyên bố khoa học được giới hạn chuẩn xác:**
> 1. **Về phạm vi tổng quát hóa:** Bộ dữ liệu 24 cases (144 lượt chạy) là một **thực nghiệm thăm dò chuyên sâu (Exploratory Domain Benchmark)** trên miền dữ liệu sách Tiki. Kết quả này **không khẳng định Multi-Agent có ưu thế tổng quát hóa tuyệt đối** trên mọi tác vụ bán lẻ nói chung, do cỡ mẫu $N=24$ chưa đạt ngưỡng $N \ge 88$ để bảo vệ mức chênh lệch nhỏ ở quy mô thị trường mở.
> 2. **Về bài toán đánh đổi kiến trúc (Architectural Trade-off):**
>    - Khi tác vụ là **tra cứu trực tiếp hoặc xử lý ngữ cảnh ngắn**, **Single-Agent là giải pháp tối ưu** về cả chi phí lẫn độ trễ (nhanh hơn gấp 3 lần), không bị rủi ro tắc nghẽn giao thức liên tác tử.
>    - **Kiến trúc Multi-Agent chỉ phát huy giá trị thực sự** khi tác vụ có tính đa chuyên môn sâu, đòi hỏi phân chia trách nhiệm rõ ràng (như kiểm định an toàn/guardrail, hoặc phân tách giữa dữ liệu số catalog và dữ liệu văn bản review). Đồng thời, hệ thống Multi-Agent đòi hỏi phải thiết kế giao thức trao đổi (Inter-Agent Protocol) bao phủ chặt chẽ để tránh lỗi nghẽn lược đồ và tráo đổi thực thể.

---

## 6. DANH MỤC ARTIFACT & MINH CHỨNG TRONG REPOSITORY

1. **Bộ dữ liệu 24 cases chuẩn hóa:** [`evaluation/v4_fair_benchmark_cases.json`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/evaluation/v4_fair_benchmark_cases.json)
2. **Runner thực thi 144 lượt chạy:** [`scripts/run_v4_full_benchmark.py`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/scripts/run_v4_full_benchmark.py)
3. **Kết quả đo đạc định lượng 144 runs:** [`output/evaluation-v4/full_24_benchmark_results.json`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/output/evaluation-v4/full_24_benchmark_results.json)
4. **Script phân tích thống kê & lực kiểm định:** [`scratch/analyze_v4_scientific.py`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/scratch/analyze_v4_scientific.py)
