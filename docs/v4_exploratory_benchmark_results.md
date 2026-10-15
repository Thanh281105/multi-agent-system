# Báo Cáo Thực Nghiệm v4: Đánh Giá Thăm Dò Đối Chiếu Multi-Agent vs. Single-Agent

> [!IMPORTANT]
> **ĐỊNH VỊ BÁO CÁO & PHẠM VI ÁP DỤNG:**
> 1. **Tư liệu thử nghiệm thăm dò (Exploratory Prototype Benchmark):** Báo cáo này ghi lại kết quả thử nghiệm thăm dò trên kịch bản độc lập (`scripts/run_v4_full_benchmark.py`). **Không dùng tài liệu này làm bằng chứng đánh giá chính thức cho runtime production** (`app/v2`), và **không đưa runner v4 vào CI** (CI tiếp tục duy trì runner chuẩn `run_real_multi_agent_benchmark.py` theo `.github/workflows/ci.yml:85`).
> 2. **Giới hạn của bộ thẩm định (LLM Judge):** Bộ chấm LLM chỉ đối chiếu câu trả lời với danh sách claim rút gọn (`required_facts`), **không đối chiếu trực tiếp với toàn bộ snapshot nguồn** (`products.jsonl`, `reviews.jsonl`, `sources.json`). Do đó, các verdict chỉ mang tính tham khảo đánh giá tương đối giữa hai prompt pipeline.

Tài liệu này trình bày toàn bộ kết quả thực nghiệm **144 lượt chạy (24 ca kiểm thử $\times$ 2 hệ thống $\times$ 3 lần lặp lại độc lập)** trên bộ benchmark v4 ([`evaluation/v4_fair_benchmark_cases.json`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/evaluation/v4_fair_benchmark_cases.json)) đối chiếu giữa mô hình **Multi-Agent độc lập** và **Single-Agent Baseline (SA)**.

---

## 1. TÓM TẮT ĐIỀU HÀNH & KẾT QUẢ ĐO LƯỜNG CHÍNH XÁC

* **Quy mô thực nghiệm:** 24 cases phân tầng độ phức tạp $\times$ 2 hệ thống $\times$ 3 lần lặp lại độc lập = **144 lượt thực thi đo đạc**.
* **Mô hình nền tảng:** `gpt-5.4-mini` (áp dụng đồng nhất cho cả 2 hệ thống, nhiệt độ sinh $T = 0.0$ để tối đa hóa tính tái lập).
* **Kiến trúc Multi-Agent cải tiến:** **OpenClaw-Inspired Architecture** — Sub-Agents chạy song song (`asyncio.gather`), Router tự hành xử lý linh hoạt từ ngữ văn học tiếng Việt, Synthesizer trực diện có cấu trúc và khuyến nghị mua sắm thực tế.
* **Bộ dữ liệu kết quả gốc:** [`output/evaluation-v4/full_24_benchmark_results.json`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/output/evaluation-v4/full_24_benchmark_results.json).

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ BẢNG TỔNG KẾT ĐO LƯỜNG CHÍNH XÁC (N = 24 CASES | 144 RUNS ĐỘC LẬP)                     │
├───────────────────────────────┬──────────────────────────┬─────────────────────────────┤
│ Chỉ số đo lường               │ Đợt Đo Ban Đầu (v4 cũ)   │ Đợt Nâng Cấp OpenClaw MAS   │
├───────────────────────────────┼──────────────────────────┼─────────────────────────────┤
│ Điểm Fact trung bình (Macro)  │ SA: 99.31% | MA: 99.31%  │ SA: 98.96% | MA: 100.00%    │
│ Đếm trực tiếp Verdict (Micro) │ SA: 128/129 | MA: 128/129│ SA: 126/129 | MA: 129/129   │
│ Chênh lệch Fact (Delta Δ)     │ Δ = 0.00%                │ Δ = +1.04% (nghiêng về MA)  │
│ Độ trễ TB Pipeline sinh câu   │ SA: 1.334 s | MA: 5.644 s│ SA: 1.428 s | MA: 6.746 s   │
│ Tỷ lệ đánh đổi độ trễ         │ SA nhanh hơn 4.23 lần    │ SA nhanh hơn 4.73 lần       │
│ Lượng Token TB Pipeline sinh  │ SA: 1.334 tok|MA:3.425 tok SA: 1.332 tok|MA:3.706 tok │
│ Tỷ lệ tiêu thụ Token thực tế  │ MA tốn gấp 2.57 lần SA   │ MA tốn gấp 2.78 lần SA      │
│ Đối đầu ghép cặp (Win-Tie-Loss│ 1 SA Win | 22 Tie | 1 MA │ 0 SA Win | 23 Tie | 1 MA Win│
│ Khoảng tin cậy 95% (CI)       │ [-2.08%, +2.08%]         │ [-1.11%, +3.20%]            │
│ Kiểm định t-ghép cặp (t-stat) │ t = 0.000 (p = 1.000)    │ t = 1.000 (p = 0.328)       │
│ Ca phân định thắng thua       │ 1 SA (c07) | 1 MA (c06)  │ MA thắng ca Catalog 01      │
└───────────────────────────────┴──────────────────────────┴─────────────────────────────┘
```

> [!NOTE]
> *Lưu ý về số liệu micro và kết luận thống kê:*
> - **Đợt đo ban đầu (baseline v4 cũ):** Mỗi bên đạt **128/129 verdicts**. SA trượt 1 fact ở `fair_tier2_cross_07` (do judge phạt khi nêu thêm review ngoài blueprint), MA trượt 1 fact ở `fair_tier2_cross_06` (do judge phạt nhận xét phàn nàn). Sau đối chiếu snapshot, cả hai đều có trong nguồn thật.
> - **Đợt nâng cấp mới (OpenClaw MAS):** MA đạt **129/129 verdicts** (100.00%). SA đạt **126/129 verdicts** do trượt `fact_t1_c01_name` ở cả 3 repeats của ca `fair_tier1_catalog_01` (thiếu hậu tố ấn bản `(Tái Bản 2022)`).
> - **Đánh giá học thuật trung thực:** Kết quả mới ghi nhận **1 ca MA thắng, 23 ca hòa**, kiểm định t-ghép cặp cho thấy $p = 0.328$ (chưa có ý nghĩa thống kê ở mức $\alpha = 0.05$). Đồng thời, MA chậm hơn khoảng **4.73 lần** và tiêu tốn gấp **2.78 lần token**. Do đó, kết quả này **chưa chứng minh Multi-Agent có ưu thế tổng quát vượt trội** và chỉ dừng lại ở mức khảo sát thử nghiệm thăm dò, không phải bằng chứng cho runtime production.

---

## 2. THẨM ĐỊNH ĐỐI CHIẾU NGUỒN SNAPSHOT (SNAPSHOT-GROUNDED ADJUDICATION)

Qua việc kiểm tra đối chiếu trực tiếp giữa câu trả lời sinh ra và file dữ liệu nguồn Tiki (`products.jsonl`, `reviews.jsonl`, `sources.json`), hai ca lệch điểm trong lần chạy tự động thực chất là **sai số âm tính giả (false negative) của bộ chấm LLM Judge**:

### 2.1. Ca `fair_tier2_cross_07` (Sách *Bill Marriott*):
* **Lỗi bộ chấm:** Bộ chấm đánh trượt Single-Agent ở Repeat 0 với lý do: *"Yêu cầu chỉ cần nêu nhận xét khớp với 'Sách hay.', nhưng câu trả lời thêm nhiều nhận xét khác không được xác nhận từ fact này"*.
* **Đối chiếu dữ liệu Snapshot thật (`reviews.jsonl` cho sản phẩm `101818123`):**
  - Review `10771683`: `"Sách hay."`
  - Review `11164359`: `"sách rất đẹp,rất hay"`
  - Review `11327265`: `"Giao hàng nhanh"`
  - Review `11997823`: `"Sách mới và đẹp, không bị quăn mép hay bị rách Ship nhanh đóng gói oke 10 điểm"`
* **Kết luận Adjudication:** Câu trả lời của Single-Agent tổng hợp thêm các ý "sách đẹp", "giao hàng nhanh" là **hoàn toàn có thật và chính xác 100% trong snapshot nguồn** của sản phẩm đó. Việc bộ chấm đánh trượt là do bộ chấm chỉ nhận fact blueprint mà không nhận toàn bộ review nguồn. Single-Agent đạt **100%** ở ca này.

### 2.2. Ca `fair_tier2_cross_06` (Sách *Những Giấc Mơ Ở Hiệu Sách Morisaki*):
* **Lỗi bộ chấm:** Bộ chấm đánh trượt Multi-Agent ở Repeat 2 vì: *"Phần phàn nàn không nêu đúng trọng tâm 'hộp bị móp méo' như yêu cầu; câu trả lời chỉ nói phàn nàn nhẹ về nội dung chưa cuốn hút"*.
* **Đối chiếu dữ liệu Snapshot thật (`reviews.jsonl` cho sản phẩm `197657913`):**
  - Duy nhất review `17942574` phản ánh phàn nàn, nguyên văn là: *"Hộp móp méo nhưng sách vẫn còn nguyên, giao đúng hẹn, **nội dung sách thì thấy không cuốn hút lắm**"*.
* **Kết luận Adjudication:** Câu phàn nàn mà Multi-Agent trích dẫn ("nội dung chưa cuốn hút lắm") **nằm chính xác trong cùng một câu văn của review gốc trong snapshot**! Do đó Multi-Agent không hề bịa đặt mà đã trích dẫn đúng một vế phàn nàn thực tế khác của độc giả. Multi-Agent đạt **100%** ở ca này.

### 2.3. Đợt Nâng Cấp OpenClaw MAS & Thắng Lợi Ở Ca Catalog 01 (`fair_tier1_catalog_01`):
* **Bối cảnh ca kiểm thử:** Yêu cầu tra cứu giá bán, tác giả, nhà xuất bản của *Sapiens: Lược Sử Loài Người* trong catalog. Fact blueprint bắt buộc phải chỉ ra tên đầy đủ của ấn bản được lưu trữ trong cơ sở dữ liệu Tiki: `"Sapiens Lược Sử Loài Người (Tái Bản 2022)"`.
* **Kết quả Single-Agent (3/4 facts - 75.0%):** Do chỉ tiếp nhận flat prompt mà không có schema ràng buộc, Single-Agent lặp lại cụm từ người dùng hỏi (*"Sapiens: Lược Sử Loài Người"*) mà bỏ sót hậu tố ấn bản `(Tái Bản 2022)` có trong bản ghi catalog.
* **Kết quả Multi-Agent OpenClaw (4/4 facts - 100.0%):** Catalog Specialist trích xuất toàn vẹn 11 trường thông tin theo schema nghiêm ngặt (`name: Sapiens Lược Sử Loài Người (Tái Bản 2022)`), và Synthesizer bảo toàn chính xác tên ấn bản cùng bảng thông số chuẩn. Multi-Agent đạt điểm tuyệt đối 100% qua cả 3 lần lặp lại độc lập.
* **Tổng kết toàn bộ 24 ca:** Multi-Agent đạt **100.00% Fact Coverage (129/129 verdicts)**, Single-Agent đạt **98.96% (126/129 verdicts)**, đem lại kết quả **1 MA Thắng - 23 Hòa - 0 SA Thắng**.

---

## 3. GIẢI MÃ KIẾN TRÚC: VÌ SAO MULTI-AGENT CHƯA THỂ VƯỢT TRỘI SINGLE-AGENT?

Nghiên cứu sâu vào mã nguồn của hệ thống (`app/agents/`, `app/orchestrator/`, `app/v2/`) chỉ ra 4 nguyên nhân kỹ thuật cốt lõi giải thích vì sao Multi-Agent không thể hiện ưu thế vượt trội về độ chính xác trên bài toán này:

```mermaid
graph TD
    subgraph Single-Agent Pipeline [Single-Agent: Toàn Cầu & Trực Tiếp]
        U1[Câu hỏi người dùng] --> LLM1[LLM Attention Toàn Cục]
        D1[Toàn bộ Ngữ cảnh Catalog + Review + Source] --> LLM1
        LLM1 --> A1[Câu trả lời trực tiếp: 1.33s | 1.3k tok]
    end

    subgraph Multi-Agent Pipeline [Multi-Agent: Phân Mảnh & Nối Tiếp]
        U2[Câu hỏi người dùng] --> R[Regex & Intent Router]
        R -->|Regex rớt từ khóa| F1[Nguy cơ Unsupported]
        R --> P1[ProductAgent: SQL/Tool]
        P1 -->|Lỗi limit=1| F2[Lệch Product ID]
        P1 --> R1[ReviewAgent: Aspect/Sentiment]
        R1 --> K1[KnowledgeAgent: Thư mục]
        P1 & R1 & K1 --> S[Synthesizer / Aggregator]
        S -->|Nén trừu tượng| F3[Mất chi tiết trích dẫn]
        S --> A2[Câu trả lời tổng hợp: 5.64s | 3.4k tok]
    end
```

### 3.1. Điểm nghẽn Router bị trói buộc bởi Regex cứng (`app/orchestrator/router.py`)
* Trong mã nguồn core, router heuristic phụ thuộc vào `BOOK_MARKERS = ("sách", "cuốn", "quyển", "tác giả", "nhà xuất bản", "nxb")`.
* Khi câu hỏi người dùng sử dụng ngôn ngữ tự nhiên như *"Tìm tác phẩm Becoming"*, *"Kiểm tra ấn bản Gieo hạt cùng vĩ nhân"*, router bị trượt khỏi domain sách và rơi vào nhánh `general.unsupported` (chiếm 20.8% trường hợp).
* Trích xuất tham số lọc sử dụng hàng loạt Regex cứng (`_extract_money`, `_extract_page_range`, `_extract_named_book_subject`). Nếu câu hỏi có cấu trúc đảo ngữ hoặc dùng từ đồng nghĩa, Regex không bắt được entity, làm rỗng payload gửi tới Agent.
* **Ngược lại ở Single-Agent:** Toàn bộ văn bản đi thẳng vào LLM không qua bộ lọc Regex nào, LLM xử lý mượt mà mọi cấu trúc ngữ pháp tiếng Việt.

### 3.2. Rủi ro lỗi dây chuyền do phụ thuộc tuần tự (`app/registry/default_intents.py`)
* Để phân tích review (`_review_summary`), Multi-Agent bị ép vào DAG 2 bước:
  `step_product` (ProductAgent `product.search` với `limit=1`) $\rightarrow$ lấy `product_id_from` truyền vào `step_review` (ReviewAgent).
* Trong [`app/agents/review/agent.py`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/app/agents/review/agent.py#L61-L70), ReviewAgent bắt buộc `isinstance(product_id, int)`. Nếu bước tìm kiếm sách ban đầu lấy nhầm sản phẩm hoặc trả về rỗng, ReviewAgent lập tức **thất bại hoàn toàn**.
* **Ngược lại ở Single-Agent:** Không có bước trung gian nào. LLM nhìn thấy cả tên sách lẫn danh sách review trong cùng một ngữ cảnh và tự ghép nối mà không cần phân giải ID qua 2 chặng.

### 3.3. Cơ chế kiểm soát tổng hợp quá khắt khe (`app/orchestrator/aggregator.py`)
* Trong `ResultAggregator.aggregate_async`, hệ thống chia nhỏ câu trả lời deterministic bằng regex `re.split(r"(?<=[.!?])\s+", answer)` để tạo danh mục `claim_catalog`.
* Nếu LLM tổng hợp diễn đạt lại câu văn hoặc thay đổi trật tự claims khiến tập `claim_id` không khớp tuyệt đối (`set(selected_ids) != set(claims_by_id)`), hệ thống coi là `ungrounded_synthesis` và **hủy bỏ toàn bộ câu trả lời của LLM, rơi ngược về chuỗi nối deterministic thô sơ**.
* Sự khắt khe này bảo vệ an toàn chống bịa đặt, nhưng triệt tiêu hoàn toàn khả năng lập luận linh hoạt của Multi-Agent.

### 3.4. Bẫy "Ngữ Cảnh Vừa Vặn" (In-Context Ceiling Effect)
* Tập 24 cases Tiki Books có tổng ngữ cảnh (1-3 sản phẩm, 5-10 reviews, vài dòng nguồn) chỉ khoảng **1.000 - 2.500 tokens**.
* Ngữ cảnh này nằm hoàn toàn bên trong Context Window 128k của các LLM hiện đại. Cơ chế Self-Attention toàn cục cho phép Single-Agent đối chiếu chéo (cross-reference) mọi fact tức thì trong 1 lần suy luận.
* Khi Single-Agent đã đạt trần 100% độ chính xác, Multi-Agent dù hoàn hảo cũng chỉ đạt tối đa 100%, trong khi phải chịu thêm chi phí độ trễ (chậm hơn 4.23x) và token (tốn hơn 2.57x).

---

## 4. KHI NÀO MULTI-AGENT MỚI THỰC SỰ CÓ ƯU THẾ? (ĐỊNH HƯỚNG CHO KHÓA LUẬN)

Kết quả thực nghiệm này mang lại một đóng góp học thuật quan trọng cho khóa luận tốt nghiệp: **định hình rõ ranh giới giá trị của kiến trúc tác tử thay vì phóng đại thiếu căn cứ**:

1. **Phân tách quyền hạn và Kiểm soát an toàn (Role-Based Access Control - RBAC):**
   Trong môi trường doanh nghiệp thực tế, Single-Agent không thể được cấp quyền truy cập toàn bộ các công cụ vừa đọc vừa sửa đổi (nguy cơ Prompt Injection). Kiến trúc Multi-Agent là bắt buộc để phân lập: Agent Tư vấn chỉ có quyền đọc; Agent Đặt hàng/Kho bãi chỉ có quyền ghi qua API có kiểm duyệt bảo mật riêng (`app/v2/authorization.py`).
2. **Quy mô dữ liệu vượt ngoài Context Window (Context Scalability):**
   Khi bài toán mở rộng sang phân tích 10.000 reviews của 50 cuốn sách, Single-Agent hoàn toàn bất khả thi. Khi đó, Multi-Agent theo mô hình Map-Reduce (nhiều Review Specialists quét song song từng phân mảnh rồi gửi về Synthesizer) là giải pháp bắt buộc.
3. **Phân tách công cụ chuyên biệt (Tool Specialization):**
   Mỗi agent sở hữu môi trường runtime độc lập: một agent chạy SQL truy vấn cơ sở dữ liệu quan hệ, một agent chạy Vector Search trên semantic index, một agent chạy code Python phân tích thống kê.

---

## 5. DANH MỤC MINH CHỨNG & TÀI LIỆU

1. **Bộ benchmark chuẩn hóa 24 cases:** [`evaluation/v4_fair_benchmark_cases.json`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/evaluation/v4_fair_benchmark_cases.json)
2. **Runner thực thi đo đạc trực tiếp (Autonomous Routing + Token Accounting):** [`scripts/run_v4_full_benchmark.py`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/scripts/run_v4_full_benchmark.py)
3. **Dữ liệu thực nghiệm 144 runs hoàn chỉnh:** [`output/evaluation-v4/full_24_benchmark_results.json`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/output/evaluation-v4/full_24_benchmark_results.json)
4. **Script tổng hợp & thẩm định đối chiếu:** [`scratch/human_audit_report.py`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/scratch/human_audit_report.py)
