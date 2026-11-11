# Sinh báo cáo từ bằng chứng và trạng thái artifact cũ

Từ bản sửa ngày 2026-11-06, các lệnh báo cáo yêu cầu đầu vào và output tường
minh. Không sinh số đo từ hằng số, tạo nhãn người chấm mô phỏng hoặc ghi đè
gói kết quả cũ. Các lệnh bên dưới chạy offline.

## Đối soát manifest

```powershell
python -m app.evaluation.evidence_integrity `
  --manifest evaluation/p8-publication-v1/manifest.json `
  --output output/evidence-audit/p8-20261106.json

python -m app.evaluation.evidence_integrity `
  --manifest evaluation/v3/thesis-validation-v1/locked-validation-v1/locked-validation-manifest.json `
  --output output/evidence-audit/catalog-20261106.json
```

Exit code 0: file khớp manifest. Exit code 1: báo cáo JSON liệt kê các lỗi.
Output phải là file mới. Hash được tính trên byte nguyên bản, không sửa line
ending hoặc manifest để ép kiểm tra qua. Audit chỉ chứng minh tính toàn vẹn
file, không chứng minh chất lượng, nguồn gốc nhãn người hoặc độc lập thực nghiệm.

Gói ZIP hiện tại có các vấn đề cần giữ hiển thị:

- P8 thiếu `preparation.json`; CSV dùng LF trong khi hash frozen ứng với CRLF.
- Ba file trong locked catalog set không khớp `file_digests`; số task/cluster
  đếm lại vẫn là 624/250. Cả ba hash khớp chính xác nếu phục hồi CRLF. Không
  sửa hash rồi coi như đã xác nhận bản freeze.
- Báo cáo trade-off/ablation cũ sinh từ số liệu viết sẵn, không phải các phép
  đo mới được thực thi bởi script mang tên tương ứng.
- Calibration cũ dùng 40 bộ nhãn viết sẵn trong script mô phỏng. Đồng thuận
  A/B trong fixture là 37/40 (92,5%), không phải 38/40. Nhãn này không chứng
  minh có người thật chấm mù hoặc judge đủ điều kiện publish.

Muốn khôi phục tính toàn vẹn: lấy lại artifact nguyên bản từ nơi lưu trữ,
hoặc tạo lần chạy/binding phiên bản mới. Không cập nhật manifest frozen tại
chỗ. Artifact JSON cũ được giữ nguyên để truy vết.

Có thể phục hồi sai line ending sang một thư mục mới, chỉ khi byte sau chuyển
LF/CRLF khớp đúng digest (và kích thước nếu được khai báo) đã có trong manifest:

```powershell
python -m app.evaluation.evidence_integrity `
  --manifest evaluation/v3/thesis-validation-v1/locked-validation-v1/locked-validation-manifest.json `
  --output output/evidence-audit/catalog-recovery-20261106.json `
  --recover-line-endings-to output/evidence-recovery/catalog-20261106
```

Manifest và source giữ nguyên; gói phục hồi được audit lại trước khi xuất.
Không chấp nhận file thiếu, chỉnh sửa khác newline hay hash manifest sai.
Audit vẫn ghi tình trạng source là invalid và ghi `recovery_status` riêng;
exit 0 khi phục hồi thành công. Với P8 thiếu preparation, phục hồi cả gói bị
từ chối. Phục hồi byte không xác nhận chất lượng/thời điểm freeze của thí nghiệm.

## Xuất gói báo cáo đã có

```powershell
python scripts/build_reproducibility_package.py `
  --bundle output/MY_RUN/report-v3 `
  --output output/verified-export/MY_RUN
```

Bundle phải là thư mục report v3 của harness, gồm `manifest.json`,
`analysis.json`, `report.json`, `blinded_answers.json`, `unblinding_key.json`
và `judgments.json`. Không truyền thư mục summary P8 rút gọn.

Lệnh dùng lại validator v3, chỉ nhận gói complete, kiểm tra mỗi answer có đúng
một judgment, và yêu cầu model judgments có calibration binding. Report phải
được dẫn xuất đúng từ analysis, kể cả run ID và số observation. Validator tính
lại delta, trung bình, win/tie/loss và bootstrap theo seed đã khai báo. Các metric
có trong phiếu chấm được nối qua unblinding key để tính lại theo repetition,
conversation rồi work group. File được copy và kiểm tra lại trước khi công bố
output; source bundle không bị sửa.

Output gồm evidence gốc, `summary.json`, `report.md`, `package-manifest.json`.
Metric chưa đo vẫn là null; giữ cả kết quả MA thua. `summary.json` phiên bản
`verified_report_export_v2` có `comparison_reconciliation` cho từng metric/cặp
variant: `recomputed_from_final_judgments` hoặc
`artifact_arithmetic_only_no_judgment_metric`. Ví dụ, chi phí không có trong phiếu
chấm thì chưa được tính lại từ execution receipts. Lệnh không chạy lại thí
nghiệm, xác nhận holdout độc lập hoặc bảo đảm người chấm thật chỉ vì input khai
báo `human_review`.

## Xuất so sánh kiến trúc đã đo

```powershell
python scripts/run_architectural_ablation.py `
  --bundle output/MY_RUN/report-v3 `
  --baseline sa_shared_tools_rag `
  --candidate ma_adaptive_rag `
  --output output/verified-comparison/MY_RUN
```

Entry point này xuất so sánh đã tồn tại; không còn chạy "ablation study" bằng
giá trị viết sẵn. Hai nhánh khác nhiều thành phần không cô lập tác động nhân
quả riêng của planner, router hoặc synthesizer.

`compute_ablation_delta` chỉ tính chênh lệch mô tả. Trường
`is_statistically_superior` là null và `inference_status` là `not_tested`.
Mức tăng hơn 5 pp không tự có ý nghĩa thống kê. Tỷ số mẫu số bằng 0 là null.

## Tóm tắt phiếu chấm mù đã thu thập

```powershell
python evaluation/v3/thesis-validation-v1/run_human_judge_calibration.py `
  --packet output/MY_RUN/report-v3/blinded_answers.json `
  --ratings work/human-ratings.jsonl `
  --output output/calibration/MY_RUN-summary.json
```

Mỗi dòng JSONL có định dạng sau. Đây chỉ là ví dụ contract, không phải phiếu
chấm thực nghiệm:

```json
{"opaque_answer_id":"answer_000000000000000000000001","metric":"claim_support","reviewer_a_id":"reviewer_alpha","reviewer_b_id":"reviewer_beta","adjudicator_id":"reviewer_gamma","human_a":1,"human_b":0,"judge":1,"adjudicated":0}
```

ID phải tồn tại trong packet v3 có hash hợp lệ. Mỗi cặp answer/metric chỉ có
một dòng; ba reviewer ID khác nhau; nhãn là số nguyên 0/1. Báo số đồng thuận,
tỷ lệ, kappa và confusion matrix riêng theo metric; kappa không xác định giữ
null. Packet và file phiếu được bind bằng hash.

Output là mô tả nhãn đã nộp (`descriptive_only_not_calibration_freeze`), không
thay P8 calibration freeze. Người vận hành phải lấy mẫu, chấm độc lập, làm
mù, hòa giải và lưu phiếu gốc. Không suy sai số thực tế từ nhãn mô phỏng.

## Entry point lịch sử và thực thi hiện hành

V4 cũ là prompt pipeline riêng, với context chọn bằng provenance trong gold.
Nó chỉ đo synthesis trên context có trợ giúp từ oracle. Runner này và 14 script
ad-hoc khác đã ngừng thực thi; chạy đường dẫn cũ trả exit 2 và thông báo chuyển
sang CLI. Byte nguyên bản lưu tại `docs/legacy-scripts/*.py.txt`, kèm manifest
SHA-256. Kết quả 144 lượt cũ và artifact frozen giữ nguyên.

Đọc protocol lịch sử không đồng nghĩa được phép chạy với runtime hiện tại.
Loader giữ nguyên binding `low/medium/high` và các model lịch sử; preflight live
chỉ chấp nhận đúng model/reasoning mà composer hỗ trợ và judge model riêng.
Protocol frozen cũ sẽ bị từ chối live nếu không tương thích. Tạo protocol/binding
mới từ source đã commit; không sửa frozen JSON hoặc nới preflight để ép chạy.

CLI duy trì là `python -m app.evaluation.v3_cli` (P7) và
`python -m app.evaluation.benchmark_cli` (P8). Xem `--help` trước khi cấu hình:
chạy live cần database, quyền mạng và ngân sách tường minh theo CLI. Shopper
không nhận `resolved_product_ids` từ gold; merchant fixture giữ ID hành động
được cấp quyền. SA/MA dùng cùng runtime graph và structured contracts.

Đợt sửa không tạo corpus, phiếu người chấm hoặc kết quả provider mới. Benchmark
cùng public runtime, tập xác nhận mới và chấm mù thật vẫn cần được thực hiện
trước khi kết luận về lợi ích routing, retrieval hoặc concurrency. Regression
kiểm tra nhiều fact không thay thế đo chất lượng trên người dùng thật.
