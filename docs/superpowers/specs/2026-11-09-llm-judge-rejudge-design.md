# Thiết kế: chấm lại câu trả lời đã lưu bằng LLM judge (hướng B)

Ngày: 2026-11-09. Trạng thái: chờ người dùng duyệt spec.

## 1. Mục tiêu

Trả lời hai câu hỏi hội đồng chắc chắn hỏi mà không cần người chấm:

1. Điểm bộ v4 có bền khi judge được cấp nguồn thật (snapshot) thay vì chỉ
   `required_facts` rút gọn không? (Đã biết hai âm tính giả: `fair_tier2_cross_06`,
   `fair_tier2_cross_07`.)
2. Judge Luna có nhất quán khi chấm lại cùng 720 câu trả lời P8 không? Kết luận
   so sánh ghép cặp có đổi giữa hai lần chấm không?

Người dùng chọn dùng LLM-as-judge, không cần người chấm, và dùng `gpt-6-luna`.

## 2. Phạm vi

**B1 — v4 chấm lại có nguồn.** 144 câu trả lời trong
`output/evaluation-v4/full_24_benchmark_results.json` (24 ca × 2 hệ thống × 3 lần).
Judge Luna nhận: câu hỏi, câu trả lời, `required_facts` của ca (từ
`evaluation/v4_fair_benchmark_cases.json`) và các bản ghi snapshot liên quan
(`products.jsonl`, `reviews.jsonl`, `sources.json`). Rubric: một fact được chấp
nhận nếu có mặt đúng nghĩa; thông tin bổ sung không bị phạt nếu có trong snapshot
và bị đánh dấu riêng nếu không có căn cứ.

**B2 — P8 test-retest.** 720 câu trả lời trong `blinded_answers.json` của gói P8
gốc, chấm lại bằng cùng cấu hình judge. Đo độ đồng thuận giữa lần chấm gốc và
lần chấm lại (tỷ lệ trùng, Cohen's kappa cho các nhãn rời rạc), rồi tính lại bảng
so sánh ghép cặp với lần chấm lại.

**Ngoài phạm vi:** sinh lại câu trả lời; sửa hệ thống; holdout mới; judge model
thứ hai (người dùng chọn Luna cho cả hai); người chấm; ghi đè bất kỳ artifact
frozen nào.

## 3. Giới hạn trung thực phải nêu trong mọi báo cáo

- Dùng cùng một model làm judge thì không phát hiện được thiên lệch chung của
  model đó. B2 đo độ ồn của judge, không đo độ đúng.
- Judge chưa được người hiệu chuẩn.
- B1 chạy trên bộ v4 đã được xem để định hướng sửa hệ thống, nên là thăm dò.
- Kết quả có thể bất lợi cho MA; vẫn báo cáo đúng như đo được.

## 4. Thiết kế kỹ thuật

- Mã mới nằm trong `scripts/` hoặc `app/evaluation/` theo cấu trúc hiện có, tái
  dùng `v3_judge`/`variant_runtime` và gọi model qua `ModelRuntime`/ledger ngân
  sách hiện có thay vì tự gọi HTTP.
- Đầu ra: thư mục mới ngoài các thư mục frozen (ví dụ
  `output/evaluation-v3/rejudge-20261109/`), chứa phán quyết thô, bảng đồng thuận
  và bảng so sánh tính lại, kèm manifest hash. `evidence_integrity` phải báo
  valid trên manifest mới.
- Mỗi lần chấm lưu: model/phiên bản, hash prompt, hash input, token và chi phí.
- Chạy API trong một git worktree sạch của HEAD (tránh `source_tree_dirty`, không
  đụng `report.pdf` và `graphify-out/` của người dùng); dữ liệu gitignored được
  truyền bằng đường dẫn.

## 5. Chống tốn tiền

1. Mọi mã mới có test trước (TDD) và `pytest`, `ruff`, `mypy` phải sạch.
2. Dry-run offline: lập lịch, đếm lượt và ước lượng token/chi phí tối đa từ
   prompt thật, không network.
3. Mẫu nhỏ trước (12–24 ô, gồm các ca đã biết như `cross_06`/`cross_07`): xác
   nhận schema đầu ra, parse được, chi phí thực tế gần ước lượng.
4. Chỉ chạy đủ khi người dùng duyệt con số trần ngân sách cụ thể; trần được ép
   bằng `--run-budget-usd`/ledger và có checkpoint để `resume`, không phát lại.
5. Dừng và báo ngay nếu chi phí thực tế vượt ước lượng đáng kể hoặc tỷ lệ lỗi
   parse cao.

## 6. Kiểm thử

- Đơn vị: dựng input judge từ bản ghi lưu; parse/validate đầu ra; tính đồng
  thuận và kappa; tính lại so sánh ghép cặp (đối chiếu với `analysis.json` gốc
  khi dùng lại đúng phán quyết gốc, kết quả phải trùng).
- Không gọi mạng trong test; model được thay bằng fake.
- Chấp nhận khi: bảng so sánh tính lại từ phán quyết gốc tái tạo đúng số của
  `analysis.json` (hồi quy), và đầu ra mới qua audit toàn vẹn.

## 7. Cách báo cáo kết quả

Thêm một mục vào báo cáo LaTeX và `docs/defense-qa.md`: bảng điểm v4 trước/sau
khi cấp nguồn, độ đồng thuận B2, và nêu kết luận có đổi hay không. Không đổi số
trong artifact cũ. Chỉ dùng cụm từ khẳng định mà số liệu cho phép (ví dụ "kết
luận ổn định qua hai lần chấm bằng cùng judge", không phải "judge đã được kiểm
chứng").

## 8. Rủi ro và điều phải xác minh khi lập plan

| # | Điều chưa biết | Hướng xử lý |
|---|---|---|
| R1 | Máy móc `v3_judge` có thể đòi calibration freeze đã ràng buộc hoặc từ chối chấm lại | Nếu bị chặn: viết script thăm dò độc lập trong `scripts/`, gắn nhãn exploratory, không dùng làm số công bố |
| R2 | Pricing manifest có mục `gpt-6-luna` hay không (P8 đã dùng, hash `3c57c328…`) | Đọc manifest; nếu thiếu thì dừng, không tự thêm giá |
| R3 | `run_v4_full_benchmark.py` không còn trong repo; bản ghi v4 thiếu evidence | B1 dựng nguồn từ snapshot theo `required_facts`/ca; ghi rõ cách chọn bản ghi |
| R4 | Key API, hạn mức và rate limit (P8 từng retry 60 lần) | Xác nhận ở bước mẫu nhỏ |
| R5 | Temperature/reasoning của Luna có thể làm hai lần chấm gần như y hệt | Vẫn báo cáo; nêu rõ test-retest có thể đánh giá thấp độ ồn |

## 9. Tiêu chí hoàn thành

- Test mới và toàn bộ suite sạch; commit local nguyên tử (không push).
- Đầu ra mới có manifest và qua `evidence_integrity`.
- Chi phí thực tế ≤ trần đã duyệt, ghi trong báo cáo.
- Báo cáo LaTeX và tài liệu Q&A cập nhật, PDF biên dịch lại thành công.
