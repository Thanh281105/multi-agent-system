# Báo Cáo Thực Nghiệm P8 Chính Thức (Official Publication Benchmark Report)

> **Mã số thực nghiệm:** `run_p8_mini_luna_judge_v3_rating_fix_20261014`  
> **Cam kết Protocol:** P8 Frozen Protocol (`pilot-mini-luna-judge-v3-rating-fix-20261014/protocol.v3.json`)  
> **SUT Git Revision:** `9618e03d87ebffb42bae75d14ed40f256b7156f7`  
> **Model Sinh (SUT):** `gpt-5.4-mini-2026-03-17` (`reasoning_effort: none`)  
> **Model Thẩm Định (Judge):** `gpt-6-luna` (`reasoning_effort: none`)  
> **Trạng thái:** **100.0% Hoàn Tất (720/720 cells được chấp nhận, 0 ambiguous, 0 failed)**

---

## 1. TỔNG QUAN KẾT QUẢ ĐO LƯỜNG & CHI PHÍ

Toàn bộ 720 ô quan sát độc lập (60 ca kiểm thử held-out $\times$ 4 biến thể kiến trúc $\times$ 3 repeats) đã được thực thi trên môi trường cơ sở dữ liệu và vector index chuẩn hóa, sau đó được thẩm định mù (blind evaluation) bởi mô hình thẩm định `gpt-6-luna`.

### Bảng Kế Toán Tài Nguyên & Chi Phí (Accounting Summary)

| Chỉ số | Pipeline Sinh Câu (SUT) | Pipeline Thẩm Định (Judge) | Tổng Cộng Hệ Thống |
| :--- | :---: | :---: | :---: |
| **Tổng số lượt gọi mô hình (Calls)** | 2.382 | 752 (32 dev + 720 heldout) | **3.134 calls** |
| **Lượt gọi Vector Embedding** | 405 | 0 | **405 calls** |
| **Tổng token đầu vào (Input Tokens)** | 2.318.224 | 2.357.939 | **4.676.163 tokens** |
| **Tổng token đầu ra (Output Tokens)** | 339.284 | 235.870 | **575.154 tokens** |
| **Tổng Token Toàn Phần** | 2.657.508 | 2.593.809 | **5.251.317 tokens** |
| **Chi phí thực tế (Effective Cost)** | $2.6906 | $0.2225 | **$2.9303 USD** |
| **Số lần thử lại mạng (Retries)** | 1 | 60 (Rate-limit recovery) | **61 retries** |
| **Số quan sát hợp lệ (Accepted)** | 720 / 720 | 720 / 720 | **720 / 720 (100.0%)** |
| **Số ca nhập nhằng / thất bại** | 0 | 0 | **0 (Zero-loss)** |

---

## 2. KẾT QUẢ ĐỐI ĐẦU GHÉP CẶP (HEAD-TO-HEAD COMPARISON)

Theo phân tích bootstrap cụm tác giả (Author-Cluster Bootstrap) với $N = 180$ cặp quan sát cho mỗi so sánh:

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ SO SÁNH GHÉP CẶP GIỮA CÁC BIẾN THỂ KIẾN TRÚC (N = 180 PAIRED OBSERVATIONS MỖI NHÁNH)                   │
├───────────────────────────────────┬──────────────┬───────────────────────────┬─────────────────────────┤
│ Cặp So Sánh (Baseline vs SUT)     │ Chỉ Số       │ Chênh Lệch TB (Mean Δ)    │ Khoảng Tin Cậy 95% CI   │
├───────────────────────────────────┼──────────────┼───────────────────────────┼─────────────────────────┤
│ SA Shared Tools vs MA Fixed RAG   │ Task Compl.  │ +0.0206 (+2.06%)          │ [-0.0309, +0.0730]      │
│ SA Shared Tools vs MA Fixed RAG   │ Claim Supp.  │ +0.0329 (+3.29%)          │ [-0.0216, +0.0905]      │
│ SA Shared Tools vs MA Fixed RAG   │ Latency      │ -337.29 ms                │ [-1004.46, +439.21]     │
│ SA Shared Tools vs MA Fixed RAG   │ Tokens       │ +331.81 tokens            │ [-87.17, +794.05]       │
├───────────────────────────────────┼──────────────┼───────────────────────────┼─────────────────────────┤
│ SA Shared Tools vs MA Adaptive    │ Task Compl.  │ -0.0381 (-3.81%)          │ [-0.0874, +0.0103]      │
│ SA Shared Tools vs MA Adaptive    │ Claim Supp.  │ -0.0319 (-3.19%)          │ [-0.0864, +0.0247]      │
│ SA Shared Tools vs MA Adaptive    │ Latency      │ +1238.81 ms               │ [+561.40, +2032.73]     │
│ SA Shared Tools vs MA Adaptive    │ Tokens       │ +757.68 tokens            │ [+319.75, +1235.86]     │
├───────────────────────────────────┼──────────────┼───────────────────────────┼─────────────────────────┤
│ MA Fixed RAG vs MA Adaptive RAG   │ Task Compl.  │ -0.0586 (-5.86%)          │ [-0.1019, -0.0216]*     │
│ MA Fixed RAG vs MA Adaptive RAG   │ Claim Supp.  │ -0.0648 (-6.48%)          │ [-0.1080, -0.0278]*     │
│ MA Fixed RAG vs MA Adaptive RAG   │ Latency      │ +1576.10 ms               │ [+1036.31, +2197.88]    │
├───────────────────────────────────┼──────────────┼───────────────────────────┼─────────────────────────┤
│ SA Shared Tools vs MA Adaptive    │ Task Compl.  │ -0.3992 (-39.92%)         │ [-0.5504, -0.2490]*     │
│ (Ablation Không Dùng RAG)         │ Abstention   │ -0.7438 (-74.38%)         │ [-0.8519, -0.6265]*     │
│                                   │ Claim Supp.  │ -0.3868 (-38.68%)         │ [-0.5484, -0.2346]*     │
│                                   │ Latency      │ -6140.49 ms               │ [-7026.61, -5192.06]*   │
└───────────────────────────────────┴──────────────┴───────────────────────────┴─────────────────────────┘
* Các khoảng tin cậy không chứa 0 biểu thị sự khác biệt có ý nghĩa thống kê ở mức α = 0.05.
```

### Nhận Định Khoa Học Cốt Lõi:
1. **Tuân thủ phân quyền và an toàn (100% Authorization Compliance):**
   - Cả 4 biến thể đạt tuyệt đối $\Delta = 0.0$ vi phạm phân quyền trên toàn bộ 720 quan sát.
2. **Vai trò sinh tử của Grounded RAG Retrieval:**
   - Khi tắt thành phần RAG (`ma_adaptive_no_rag`), tỷ lệ hoàn thành tác vụ tụt dốc **-39.92%**, độ chính xác claim tụt **-38.68%**, và khả năng từ chối có căn cứ tụt **-74.38%**. Điều này chứng minh kiến trúc agent không thể thay thế kiến trúc truy xuất ngữ cảnh xác thực từ snapshot.
3. **Chi phí và độ trễ đánh đổi của Adaptive Orchestration:**
   - Nhánh `ma_adaptive_rag` yêu cầu thêm trung bình $1.238$ ms độ trễ và $+757$ tokens do phải qua các bước phân loại ý định (router) và tái lập kế hoạch (replanning). Trong khi đó, nhánh `ma_fixed_rag` thực thi song song nhanh hơn đáng kể.

---

## 3. CHỨNG TỪ TÁI LẬP MÃ HÓA (CRYPTOGRAPHIC ARTIFACT INTEGRITY)

Các artifact xuất bản chính thức được niêm phong mật mã tại thư mục:  
[`output/evaluation-v3/heldout-mini-luna-judge-v3-rating-fix-20261014/p8-publication/`](file:///c:/Users/Thanh/Desktop/DACNTT/multi-agent-system/output/evaluation-v3/heldout-mini-luna-judge-v3-rating-fix-20261014/p8-publication/)

* **Publication Manifest SHA-256:** `35996008b6cc939ff6a9498d26ef312771e0c904e088e64732397ba08040997f`
* **Report Summary SHA-256:** `d3d947451ab7738be275c6661c6ae10c6318814d91771ebaf0009a993b15c7b6`
* **Calibration Verification SHA-256:** `f065f10042f5fb002b57db26567fb9eee67c5f9add4e7fc4cc61a8d0c6fbed41`
* **Preparation Artifact Set SHA-256:** `79a4f113d93d58a5d4219fd757b9ec627b3140e5acbfe17a2a122cba4ffbaaa1`

Tất cả 5 tệp dữ liệu cốt lõi (`summary.json`, `comparisons.csv`, `comparisons.svg`, `preparation.json`, `error-analysis.json`) cùng 6 tệp chứng chỉ chấm mù (`blinded_answers.json`, `judgments.json`, `unblinding_key.json`...) đã được xác thực toàn vẹn bằng SHA-256 và sẵn sàng nộp kèm phụ lục luận văn / bài báo.
