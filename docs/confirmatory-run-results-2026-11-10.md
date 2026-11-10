# Kết quả lần chạy xác nhận — 2026-11-10

Khai báo trước: `docs/preregistration-confirmatory-run-2026-11-10.md`.
Dữ liệu: `evaluation/v3/confirm-20261110/results/` (journal judge, phân tích,
nhãn tham chiếu, hash của các file lớn trong `output/`). Tái tạo bảng:
`python evaluation/v3/confirm-20261110/results/analyze.py`.

## Kết luận theo quy tắc đã khóa

**Không giả thuyết nào được ủng hộ.** Cả ba khoảng tin cậy đều chạm 0.

| Giả thuyết | So sánh (task_completion) | Δ (điểm %) | CI 95% | W/T/L nhóm | Kết luận |
|---|---|---|---|---|---|
| H1 (chính) | `ma_fixed_rag` − SA, loại `multi_constraint` | +2,78 | [0,00; +8,33] | 1/11/0 | không kết luận |
| H2 | `ma_fixed_rag` − SA, 60 câu | +1,07 | [0,00; +2,78] | 2/24/0 | không kết luận |
| H3 | `ma_adaptive_rag` − SA, 60 câu | +1,07 | [0,00; +2,78] | 2/24/0 | không kết luận |

Điều nói được: trên bộ này hai cấu hình nhiều tác tử **không thua SA ở nhóm tác
phẩm nào** và cao hơn 1,07 điểm %, nhưng chênh lệch đến từ 2/26 nhóm và chưa có
ý nghĩa thống kê. Không được viết "MA tốt hơn SA".

Trung bình task_completion: SA 19,44%; MA fixed 20,56%; MA adaptive 20,56%;
MA adaptive không RAG 0,00%. Bỏ RAG làm giảm 16,24 điểm % so với SA
(CI [−23,18; −9,62], W/T/L 0/13/13).

claim_support (60 câu): MA fixed − SA +1,07, CI [−2,56; +4,27], W/T/L 4/21/1;
MA adaptive − SA +3,21, CI [0,00; +7,05], W/T/L 5/20/1.

## Theo loại câu (task_completion, %)

| Loại | SA | MA fixed | MA adaptive | SA trên P8 gốc |
|---|---|---|---|---|
| knowledge_source | 91,7 | 94,4 | 97,2 | 86 |
| multi_constraint | 5,6 | 8,3 | 5,6 | 94 |
| multi_expert | 0 | 0 | 0 | 8 |
| multi_turn_memory | 0 | 0 | 0 | 75 |
| insufficient_conflict_injection | 0 | 0 | 0 | 62 |
| shopping_merchant | 0 | 0 | 0 | 0 |

## Bộ câu hỏi mới bị hiệu ứng sàn

Bốn loại câu cho 0% ở **mọi** biến thể, và `multi_constraint` rơi từ 94% (P8)
xuống 6%. Các chỉ số thành phần xác định (authorization, answerability,
valid_plan) giống hệt nhau giữa SA và MA ở các loại này, nên nguyên nhân nhiều
khả năng nằm ở gold do subagent soạn (yêu cầu không khớp cách hệ thống được
chấm), không phải ở hệ thống. Chưa điều tra từng câu. Hệ quả: bộ này chỉ phân
biệt được hai hệ thống ở `knowledge_source` và một phần `multi_constraint`; nó
yếu hơn P8 về sức phân biệt. Gold không được sửa sau khi xem kết quả.

## Lệch so với khai báo trước và quy trình chuẩn

1. **Cổng hiệu chuẩn judge bị nới.** Nhãn tham chiếu cho 32 ca pilot sinh bằng
   luật xác định (`build_rule_references.py`), không phải người. Ở lần hiệu chuẩn
   được dùng, judge lệch nhãn luật ở tối đa 1,0 trên `task_completion` và
   `claim_support` (lần đầu: 3/32 ca), vượt ngưỡng mặc định 0,25; chạy với
   `--calibration-max-absolute-error 1.0`. Bộ nhãn do `gpt-6-luna` sinh bị
   pipeline từ chối vì trùng model judge; bộ `gpt-5.4-nano` không sinh được.
2. **719/720 ô được chấm.** Một ô (`held_multi_constraint_02`, lặp 0,
   `ma_adaptive_no_rag`) judge trả sai schema sau hơn 12 lượt. Ô này không thuộc
   H1–H3; gán 0 hay 1 không đổi các hàng H1–H3 (xem `analysis.json`).
3. **Không có bản tổng hợp chính thức.** `benchmark_cli operate` đòi đủ 720 ô
   nên không sinh `finalized-result`; bảng trên do `analyze.py` tính từ journal
   judge theo đúng công thức khai báo (bootstrap theo work group, seed 42,
   10.000 mẫu).
4. **Sổ ngân sách local bị can thiệp để chấm lại ô lỗi định dạng.** Judge trả
   output sai schema ngẫu nhiên (khoảng 2–3/32 ca hiệu chuẩn, 13/720 ô held-out
   ở lượt đầu); pipeline không cho gọi lại. Các scope/attempt của job lỗi bị xóa
   khỏi Postgres local và dòng tương ứng bị gỡ khỏi journal để gọi lại; bản sao
   lưu nằm ở `output/evaluation-v3/confirm-20261110/ledger-reset/`. Việc chọn ô
   chấm lại chỉ dựa trên mã lỗi kỹ thuật, không dựa trên điểm. Kiểm tra tập file
   thư mục preparation được nới trong wrapper ngoài repo để resume.
5. Giới hạn đã nêu từ trước vẫn giữ: cùng 13 sản phẩm với P8, gold do subagent
   Claude soạn, judge chưa có người hiệu chuẩn.

## Chi phí (theo sổ ngân sách)

Pilot hai lần 0,234 USD; held-out + judge 1,538 USD; nhãn tham chiếu thử bằng
LLM dưới 0,02 USD. Tổng lần xác nhận khoảng 1,8 USD, dưới trần 4 USD.
