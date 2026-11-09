# Chấm lại bằng LLM judge (thăm dò, 2026-11-09)

Mã: `app/evaluation/rejudge_*.py`. Dữ liệu thô, báo cáo và manifest hash:
`evaluation/rejudge-20261109/`. Spec: `docs/superpowers/specs/2026-11-09-llm-judge-rejudge-design.md`.
Judge: `gpt-6-luna`, `reasoning_effort=none`, không retry ở tầng runtime. Chi phí
tính theo giá niêm yết trong `app/shared/pricing-manifest.json`: B1 ≈ 0,025 USD,
B2 ≈ 0,197 USD (đã gồm các lượt gọi lại), trần được duyệt 10 USD.

**Đây là phân tích thăm dò, không phải số công bố mới.** Không artifact frozen
nào bị sửa. Kết quả công bố trước đó (P8 `analysis.json`) giữ nguyên.

## Giới hạn bắt buộc phải nêu

- Cùng một model làm judge cho cả lần chấm gốc và lần chấm lại, nên **không phát
  hiện được thiên lệch chung của model**. Test-retest chỉ đo độ ồn.
- Với `reasoning_effort=none`, hai lần chấm gần như xác định; độ đồng thuận cao có
  thể **đánh giá thấp độ ồn thật**.
- Judge chưa được người hiệu chuẩn.
- Bộ v4 được thiết kế sau khi xem lỗi P8, nên chỉ là thăm dò.

## B2 — P8 chấm lại (test-retest, 715/720 câu)

5 câu bị loại vì sau nhiều lượt gọi lại judge vẫn trả output không qua validator
của P8 (2 `model_response_invalid`, 3 vi phạm quy tắc khớp fact). Các câu này
không ngẫu nhiên; **mọi số dưới đây tính trên cùng 715 câu cho cả hai lần chấm**.

| Chỉ số | Đồng thuận | Cohen's κ |
|---|---|---|
| task_completion | 97,3% | 0,946 |
| claim_support | 96,9% | 0,937 |
| answerability_abstention, useful_continuation, valid_plan | 100% | 1,0 |

Hiệu SA → MA adaptive (task_completion, ghép cặp theo work group, bootstrap
seed 42, 10.000 mẫu):

| Lần chấm | Δ | CI 95% | W/T/L (nhóm) |
|---|---|---|---|
| Gốc (715 câu) | −4,99 điểm % | [−9,26; −1,34] | 2/18/7 |
| Chấm lại (715 câu) | −6,53 điểm % | [−12,65; −1,34] | 3/17/7 |

Kết luận cho phép nói: **hướng và độ lớn của so sánh chính ổn định qua hai lần
chấm bằng cùng judge** (cả hai nghiêng về một tác tử). Không được nói "judge đã
được kiểm chứng". Lưu ý: trên tập con 715 câu, CI gốc không chứa 0, còn trên đủ
720 câu (`analysis.json`) CI là [−8,74; +1,03]. Kết luận chính thức vẫn là số
720 câu (không có bằng chứng nhiều tác tử tốt hơn).

## B1 — v4 chấm lại có cấp nguồn snapshot (144/144 câu)

Judge nhận câu hỏi, câu trả lời, `required_facts` và bản ghi nguồn dựng từ
`source_provenance` của **chính fact đó**.

| | Điểm gốc | Điểm có nguồn |
|---|---|---|
| Một tác tử | 98,96% | 88,54% |
| Nhiều tác tử | 100% | 96,53% |

13/144 lượt đổi điểm, tập trung ở 5 ca (`cross_06`, `recom_03`, `recom_04`,
`guard_01`, `guard_03`). Judge cũng liệt kê 285 "phát biểu bổ sung không có căn
cứ trong ngữ cảnh" (MA 194, SA 91; MA trả lời dài hơn).

**Không được dùng B1 như phép đo độ chính xác.** Kiểm tay hai ca SA bị trừ nặng
cho thấy lỗi thuộc về cách dựng ngữ cảnh: `recom_03` câu trả lời của SA nêu đúng
cả hai khiếu nại nhưng provenance của fact chỉ chứa một review, nên judge báo
thiếu; `recom_04` fact cần giá cuốn thứ hai không nằm trong bản ghi được cấp.
Vì vậy điểm "có nguồn" bị âm tính giả khi fact cần bằng chứng từ nhiều bản ghi,
và "285 phát biểu không có căn cứ" chỉ nghĩa là ngoài ngữ cảnh được cấp, chưa
phải bịa. Mình **không** chỉnh lại judge sau khi xem kết quả để tránh tối ưu
theo điểm.

Điều B1 chứng minh được: mức bão hòa 99%/100% của v4 **phụ thuộc thiết kế judge**
(thay judge, SA từ 98,96% xuống 88,54%), nên v4 không đủ sức phân biệt hai hệ
thống. Ca `cross_06` giảm 1,0 → 0,5 ở cả hai hệ thống vì judge phát hiện câu trả
lời gán lời khen in ấn cho review không có lời khen đó, tức judge có nguồn
nghiêm hơn judge cũ chứ không "cứu" thêm điểm. Sapiens "Tái Bản 2022" không được
cứu (blueprint vẫn yêu cầu đúng tên).

## Tái tạo

```
python -m app.evaluation.rejudge_cli dry-run  {p8|v4} --out DIR
python -m app.evaluation.rejudge_cli run      {p8|v4} --out DIR --budget-usd D --allow-network [--retry-errors]
python -m app.evaluation.rejudge_cli report   {p8|v4} --out DIR
```

`dry-run` và `report` không dùng mạng; `run` từ chối chạy nếu thiếu `--budget-usd`
hoặc `--allow-network`. Cache JSONL cho phép resume, không gọi lại ô đã có.
