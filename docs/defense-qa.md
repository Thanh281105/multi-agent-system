# Chuẩn bị bảo vệ: câu hỏi khó và câu trả lời có dẫn chứng

Tài liệu nội bộ để ôn bảo vệ. Mọi con số đều lấy từ artifact trong repo; mục
nào chưa kiểm chứng được thì ghi rõ. Không dùng tài liệu này để thêm tuyên bố
mới vào báo cáo.

## 1. Thông điệp chính (nên nói trong 2 phút mở đầu)

1. Hệ thống là trợ lý sách có **bằng chứng truy vết được**: mỗi nhận định gắn
   với evidence ID, Python giữ quyền với permission/score/citation, model chỉ
   chọn plan và fact ID trong schema giới hạn (`docs/architecture.md`).
2. Nhóm **tự kiểm tra và loại bỏ số liệu không đáng tin** (số viết sẵn, nhãn
   người chấm mô phỏng) thay vì dùng chúng. Đây là điểm mạnh về phương pháp.
3. Kết luận khoa học **có điều kiện**: MA đánh đổi 4,73× thời gian và 2,78×
   token để đổi lấy +1,04 điểm trên bộ 24 câu; chưa chứng minh vượt trội tổng quát.

## 2. Bức tranh đầy đủ về kết quả (hai bộ đánh giá khác nhau)

| Bộ | Quy mô | Kết quả chính | Ghi chú |
|---|---|---|---|
| v4 (24 câu, `evaluation/v4_fair_benchmark_cases.json`) | 144 lượt | SA 98,96% / MA 100% fact coverage; 1 MA thắng, 23 hòa; p = 0,328 | Bão hòa, không phân biệt được hai hệ thống. Đây là bộ báo cáo LaTeX dùng |
| P8 held-out (60 hội thoại / 27 nhóm tác phẩm × 4 biến thể × 3 lần = 720 ô) | 720 ô, chi phí ≈ 2,93 USD | Task completion trung bình: SA 45,2%, MA fixed 47,2%, MA adaptive 41,4%, MA không RAG 5,2% | Khó hơn nhiều; không biến thể nào quá ~47% |

Số trên lấy từ `analysis.json` của gói P8 gốc (`output/evaluation-v3/heldout-mini-luna-judge-v3-rating-fix-20261014/p8-publication/scored-artifacts/`),
đã đối chiếu ngày 2026-11-09. So sánh chính đã đăng ký (SA shared tools vs MA
adaptive): Δ task completion = −3,8% (CI 95% [−8,7%; +1,0%]), thắng/hòa/thua
theo nhóm tác phẩm = 2/18/7. Nghĩa là **không có bằng chứng MA adaptive tốt hơn
SA; ước lượng điểm còn nghiêng nhẹ về SA**. Ngoài ra MA adaptive thấp hơn MA fixed
−5,86% (CI không chứa 0).

Điểm cần chủ động nói trước hội đồng: trên bộ P8 khó hơn, tỷ lệ hoàn thành chỉ
khoảng 41–47% và kiến trúc adaptive không vượt SA. Đừng để hội đồng tự phát
hiện. Cách trình bày trung thực: v4 là bộ thăm dò đã bão hòa; P8 cho thấy còn
nhiều việc ở retrieval và độ phủ nguồn.

Lưu ý: `docs/p8_error_analysis_and_fair_benchmark.md` có biểu đồ "39/60 ca cùng
trượt, MA thắng 2, SA thắng 1". Con số này **chưa được kiểm chứng** với
`analysis.json` (thống kê gốc là 2/18/7 trên 27 nhóm) nên không dùng khi bảo vệ.

## 3. Câu hỏi khó có thể gặp

**Q1. Vì sao MA không vượt trội rõ ràng so với SA?**
Dữ liệu v4: chênh lệch nằm ở đúng 1/24 câu (hậu tố "Tái Bản 2022" của
*Sapiens*), CI 95% = [-1,11%; +3,20%] gồm cả 0. Điểm đã gần trần nên khó có
chênh lệch. Nhóm 2 và 3 (kết hợp chuyên gia) hai bên đều 100%. Trả lời trung
thực: bộ này chưa đủ sức phân biệt; cần bộ câu khó hơn (`latex/03-danh-gia.tex`,
mục "Mức chắc chắn của kết luận").

**Q2. Ưu thế ở câu *Sapiens* có thật do multi-agent không?**
Chưa chắc. Chuyên gia catalog được hướng dẫn nhấn mạnh tên đầy đủ, còn SA được
yêu cầu trả lời ngắn. Đây có thể là khác biệt prompt, không phải số lượng agent.
Phép đối chứng đề xuất: cho SA cùng chỉ dẫn bảo toàn tên ấn bản rồi thử trên
bộ câu mới (`latex/03-danh-gia.tex`). Chưa thực hiện.

**Q3. Bộ câu v4 được thiết kế sau khi xem lỗi P8 và MA được "nâng cấp" sau đó. Có phải tuning trên tập test không?**
Đây là rủi ro thật. `docs/p8_error_analysis_and_fair_benchmark.md` thiết kế bộ
v4 từ phân tích lỗi P8, và `docs/v4_exploratory_benchmark_results.md` ghi nhận
"đợt nâng cấp OpenClaw MAS" chạy lại trên cùng 24 câu. Vì vậy v4 chỉ nên được
trình bày là **thăm dò (exploratory)**, đúng như tài liệu tự định vị. Kết luận
cuối phải dựa trên tập xác nhận chưa dùng để sửa hệ thống (chưa có).

**Q4. Bộ chấm LLM có đáng tin không? Nhãn người chấm đâu?**
Không có nhãn người thật. Calibration cũ là mô phỏng (40 bộ nhãn viết sẵn) nên
đã bị loại khỏi kết luận. Bộ phiếu chấm mù cho hai người chấm đã chuẩn bị nhưng
để trống. Ngày 2026-11-09 đã chấm lại bằng cùng judge Luna (xem
`docs/rejudge_llm_judge_results.md`): trên 715/720 câu P8, đồng thuận 97,3%
(κ = 0,946) với task_completion và 96,9% (κ = 0,937) với claim_support; hiệu
SA → MA adaptive là −4,99 điểm % ở lần chấm gốc và −6,53 điểm % ở lần chấm lại
(cùng 715 câu, cùng hướng). Nói đúng: **so sánh ổn định qua hai lần chấm bằng
cùng judge**. Không nói judge đã được kiểm chứng: cùng model không phát hiện
thiên lệch chung, `reasoning_effort=none` có thể làm đánh giá thấp độ ồn, và
chưa có người hiệu chuẩn.

**Q5. Gói P8 có toàn vẹn không?**
Audit `evidence_integrity` ngày 2026-11-09: bản copy trong
`evaluation/p8-publication-v1/` **thiếu** `preparation.json` nên báo invalid.
Nhưng bản chạy gốc trong `output/.../p8-publication/p8-report/` có đủ file
(10,6 MB) và **audit hợp lệ, 0 lỗi**; hash manifest `35996008…997f` khớp hash
công bố trong báo cáo, và bốn file chung giống hệt bản copy. Vậy lỗi là bản
copy thiếu file, không phải bằng chứng bị sửa. Việc cần làm: bổ sung đúng file
gốc vào bản nộp (không tạo mới). Audit chỉ chứng minh toàn vẹn file, không
chứng minh chất lượng hay độ độc lập của nhãn. Catalog set cũng hợp lệ.

**Q6. Con số 100% có nghĩa hệ thống chính xác tuyệt đối không?**
Không. 100% nghĩa là mọi `required_facts` được judge cũ chấp nhận trên 24 câu.
Khi chấm lại cùng 144 câu trả lời bằng judge được cấp bản ghi nguồn, điểm là
88,5% (SA) và 96,5% (MA), nên mức bão hòa phụ thuộc thiết kế judge. Đừng dùng
hai số mới này làm độ chính xác: kiểm tay cho thấy có âm tính giả do ngữ cảnh
chỉ gồm bản ghi của từng fact (ví dụ `recom_03`, `recom_04`). Điều chắc chắn là
v4 không đủ sức phân biệt hai hệ thống. Cả hai bên cũng được cấp sẵn thông tin
liên quan, nên chưa đo khả năng tự tìm trong kho lớn.

**Q7. Hệ thống có an toàn/phân quyền không?**
P8: cả 4 biến thể đạt 0 vi phạm phân quyền trên 720 ô. Kiến trúc: Agent
Gateway kiểm soát tool/capability/permission; conversation/turn owner-bound
(`README.md`, `docs/architecture.md`). Số "0 vi phạm" lấy từ `analysis.json`
của gói P8 gốc (authorization = 1,0 ở cả 4 biến thể), đã qua audit toàn vẹn.

**Q8. RAG đóng góp bao nhiêu?**
So sánh cùng kiến trúc (MA adaptive có RAG → MA adaptive không RAG): task
completion giảm 36,1% (CI 95% [−51,2%; −21,6%]), claim support giảm 35,5%, khả
năng từ chối có căn cứ giảm 74,4% (CI [−84,9%; −62,3%]). Đây là bằng chứng mạnh
nhất của dự án: **phần truy xuất có căn cứ quan trọng hơn số lượng agent**.
Không dùng con số −39,92% trong báo cáo P8: baseline của nó là SA, không phải MA
có RAG, nên lẫn hai yếu tố (kiến trúc và RAG). Ngoài ra 312/720 ô không có
citation nên citation coverage chỉ đo được trên 408 ô; document recall không
được đo.

**Q9. Dữ liệu là thật không? Có phản ánh thị trường hiện tại không?**
Là snapshot lịch sử Tiki Books (200 sản phẩm, 1.773 review, truy xuất
2026-08-24), không phải feed trực tiếp. Corpus kiến thức chỉ 20 nguồn, ánh xạ
20/200 sản phẩm (20 khớp chính xác, 17 nhập nhằng, 163 chưa khớp). Phải nói rõ
giới hạn này; không tuyên bố phản ánh giá, tồn kho, xu hướng hiện tại.

**Q10. Phần mềm đã được kiểm thử thế nào?**
Ngày 2026-11-09 chạy trên máy dev: pytest 1.418 pass, 0 fail (sau khi thêm bộ chấm lại: 1.456 pass, 0 fail) (có PostgreSQL
16.15 dùng một lần), ruff và mypy (176 file) sạch, frontend: oxlint, `tsc -b`
và vitest 101/101 đều qua. Chưa kiểm Redis/Qdrant thật, chưa load/soak test.
Lưu ý: `latex/03-danh-gia.tex` ghi 1.416 test, lệch 2 so với lần chạy này;
cần thống nhất con số trước khi nộp (xem mục 5).

**Q11. Chi phí vận hành?**
P8 tốn ≈ 2,93 USD cho 720 ô (sinh 2,69 USD + judge 0,22 USD), 3.134 lượt gọi
model. Trong v4, MA tốn token gấp 2,78 lần và chậm 4,73 lần. Thứ tự chạy chưa
cân bằng ngẫu nhiên nên số đo thời gian chỉ mang tính tham khảo.

**Q13. Vì sao chưa có holdout độc lập? P8 có phải holdout không?**
P8 là held-out đóng băng trước khi chạy hệ thống: 60 hội thoại held-out tách
khỏi 20 hội thoại dev, gold sinh tự động trước SUT (`gold_origin:
automated_pre_sut_spec`). Nhưng P8 đã được phân tích lỗi và v4 thiết kế sau đó,
nên không còn là xác nhận độc lập cho phiên bản đã sửa. Một bộ thứ hai độc lập
chưa làm được vì pipeline chính thức đóng cứng 80/60/20 hội thoại và 264 fact,
và dữ liệu hiện có chỉ cho 13 sản phẩm held-out (20/200 sản phẩm có nguồn kiến
thức khớp chính xác). Câu hỏi mới viết trên cùng 13 sản phẩm chỉ là bằng chứng
tái lập, không phải độc lập. Muốn độc lập thật cần thêm nguồn tri thức, lập chỉ
mục lại và nới các hằng số đã đóng băng. Đây nằm trong Q12 (1).

**Q12. Nếu làm tiếp, bạn sẽ làm gì?**
(1) Tập xác nhận mới chưa dùng để sửa hệ thống; (2) đối chứng SA cùng chỉ dẫn
bảo toàn tên; (3) người chấm mù thật cho một mẫu; (4) tăng độ phủ corpus và
ánh xạ tác phẩm; (5) cân bằng thứ tự chạy. Đây khớp với "Hướng phát triển" của
báo cáo (`latex/04-ket-luan.tex`).

## 4. Kịch bản demo (chưa chạy trong phiên chuẩn bị này)

`scripts/strict_v2_demo_smoke.py` yêu cầu một deployment demo đã chuẩn bị và các
biến `CI_GATEWAY_KEY`, `CI_EXPECTED_CATALOG_VERSION_ID`,
`CI_EXPECTED_CORPUS_VERSION_ID`, `CI_EXPECTED_INDEX_MANIFEST_ID`; script tạo
hội thoại và có thể gọi model provider. Cần chạy thử trước ngày bảo vệ, không
chạy lần đầu tại phòng bảo vệ.

Gợi ý luồng demo (≈ 5 phút), cần xác nhận khi chạy thử:
1. Tra cứu một cuốn sách + giá/rating, chỉ ra evidence và citation trong UI.
2. Câu hỏi kết hợp catalog và knowledge (ví dụ dạng `held_multi_constraint`).
3. Yêu cầu ngoài phạm vi dữ liệu, hệ thống từ chối hoặc hỏi lại có căn cứ.
4. Mở trace/agent execution để cho thấy phân rã tác vụ.

## 5. Việc cần chốt trước ngày bảo vệ

- Số test: báo cáo LaTeX và lần chạy cuối 2026-11-09 đều là 1.456 (đã thống nhất).
- Bổ sung `preparation.json` gốc vào bản nộp P8 (copy nguyên byte từ
  `output/.../p8-report/`, rồi audit lại), hoặc nêu rõ bản nộp dùng bản gốc.
- Sửa `docs/p8_official_publication_benchmark_report.md`: nhãn ablation "SA vs
  MA Adaptive (Không RAG)" gây hiểu nhầm (xem Q8); nên thêm hàng so sánh cùng
  kiến trúc (MA adaptive vs MA adaptive không RAG).
- Chạy thử demo trên môi trường thật và ghi lại kết quả.
- Nếu muốn có số liệu mạnh hơn: cần duyệt ngân sách API để chạy tập xác nhận
  mới; không thể thay bằng chỉnh số hoặc nhãn giả lập.
