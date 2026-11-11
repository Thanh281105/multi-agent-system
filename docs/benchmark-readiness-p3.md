# P3: chuẩn bị benchmark, chưa chạy API

Ngày lập: 2026-11-06 (Asia/Saigon). P3 tiếp nối P2. Người dùng yêu cầu **API test sau**.
Không có kết quả model mới, điểm benchmark mới, nhãn người thật hoặc holdout mới
được tạo trong đợt này. Bộ dữ liệu, protocol, judge và ngân sách đã freeze vẫn giữ
nguyên. Các khuyến nghị nghiên cứu dưới đây là cơ sở thiết kế; không phải bằng
chứng rằng hệ thống này đã đạt kết quả của các bài nghiên cứu.

## Thay đổi có thể kiểm chứng offline

- Live factory yêu cầu đúng repository gốc, đúng đường dẫn module đang import,
  HEAD hợp lệ, working tree sạch và không có runtime Python/JSON bị Git ignore.
  Hash SHA-256/size của toàn bộ file tracked được ghi vào source identity; từ
  chối symlink, đường dẫn thoát root và file `.env` được track. `.env` ignored
  không nằm trong manifest. P7 lưu sidecar trước khi chạy và kiểm tra lại source
  sau khi chạy; không xuất kết luận thành công nếu source thay đổi. P8 dùng chung
  admission factory, nhưng chưa có kiểm tra sidecar đầu/cuối tương tự P7.
- Profile P7 có input bound 12.000 được truyền vào packing và full structured
  envelope check trước quote/transport. Bound này là ước lượng bảo thủ bằng byte,
  không phải tokenizer chính xác. Giới hạn chung 16.000 và judge 22.000 vẫn là
  trần; override chỉ được phép thu hẹp. Không tăng output budget để lấy điểm.
- P7 pilot/resume có egress cần `--run-budget-usd`; số hữu hạn, dương và không vượt
  reservation cap của protocol. Đây là giới hạn tài khoản ledger bổ sung; đặt
  thấp có thể làm run partial, không bảo đảm đủ tiền hoàn tất schedule. Tài khoản
  cũ có cấu hình khác có thể bị từ chối; dùng DB evaluation riêng thay vì sửa
  ledger lịch sử. P8 giữ cap theo protocol cũ, chưa thêm cờ operator budget.
- CLI readiness đọc lock, kiểm tra phiên bản package đã cài, validate protocol,
  lập preview schedule/analysis plan, kiểm tra binding source/mapping, và tạo
  queue các mapping chưa có source. Không đọc key, kết nối DB hoặc gọi provider.
- CLI human-review nhận **packet mù thật**, tạo hai thứ tự đọc độc lập và sáu
  dòng rubric/answer/reviewer. Mọi score/comment ban đầu là null. Không xuất
  unblinding key; tên slot không xác nhận người thật đã chấm. Output không ghi
  đè. Đây là bộ giao việc, chưa phải calibration artifact hoặc labels đã được
  bộ báo cáo chấp nhận.

## Dùng công cụ offline

Chạy trong môi trường development đã cài đúng `requirements-dev.lock` (gồm
`packaging`). Output phải là thư mục mới ở ngoài source tree để không tự làm
working tree dirty. Không dùng thư mục kết quả P7/P8 lịch sử.

```powershell
$root = (Get-Location).Path
$out = Join-Path (Split-Path $root -Parent) 'p3-readiness-local'
python -m app.evaluation.benchmark_readiness inspect --project-root $root --output $out
```

ZIP không chứa `.git`. Sau giải nén, dùng checkout riêng và commit source đã
review, rồi chạy inspect. Git revision mới khác revision của bản kiểm chứng;
phải freeze protocol/identity mới cho checkout đó, không sao chép revision cũ
vào manifest. Giữ sạch tree trong suốt run. Không lưu output mới trong source.

Sau này, khi có packet thật:

```powershell
python -m app.evaluation.benchmark_readiness human-review --packet '<packet-mu-that.json>' --output '<thu-muc-moi>'
```

Hai người thật chấm độc lập trước khi xem điểm model hoặc nhãn người còn lại;
người thứ ba phân xử bất đồng và giữ nguyên submission gốc. Dùng rubric/gold
đã được kiểm tra bởi con người. Sáu chiều đánh giá là hoàn tất yêu cầu, biết trả
lời/từ chối đúng, claim có bằng chứng, đúng quyền hạn, kế hoạch hợp lệ và bước
tiếp theo hữu ích. Không tự suy ra một chiều từ điểm của chiều khác. Thiếu bằng
chứng chấm phải để null và giải thích. Packet V3 hiện mang split `held_out`;
không dùng nhãn từ final holdout để chỉnh judge. Hiệu chuẩn judge phải thực hiện
trên development riêng và freeze trước final holdout.

## Kế hoạch đo và phần còn lại

| Hạng mục | Đã làm | Cần bằng chứng tiếp theo |
|---|---|---|
| Source/provenance | Admission, hashes, P7 drift check | Run thật từ checkout sạch |
| So sánh P1/P2/P3 | Plan paired, primary metric/comparison rõ | Pilot có API, quality/cost/latency, missingness và cluster CI |
| Human/judge | Công cụ tạo phiếu mù, không tạo nhãn | Hai người thật, adjudication, calibration development |
| Corpus | Audit 20 exact, 17 ambiguous, 163 unmatched; queue 180 | Xác minh work/edition, nguồn sơ cấp, use basis; publish index mới |
| Holdout | Ghi rõ bộ đã xem chỉ dùng development | Bộ mới độc lập, cluster theo work, freeze trước chạy |
| Adversarial/vận hành | Test offline hiện có và admission/budget regression | Prompt injection có model, retry/disconnect/load/multiworker thật |
| Routing/reranker/concurrency | Giữ nguyên để tránh tuning không có dữ liệu | Ablation paired cùng điều kiện và Pareto quality/cost/latency |

Preview là **development pilot**, không phải phép xác nhận MA vượt SA. Primary
metric là task completion; comparison chính là SA shared tools RAG với MA
adaptive RAG. Các so sánh còn lại là exploratory, chưa điều chỉnh multiple testing.
Repeat của cùng work phải cluster, không coi là task độc lập. Giới hạn reservation
tính từ số turn là upper bound, không phải dự báo chi phí hoặc hóa đơn.

Queue không chứa URL tự đoán; chưa có nguồn mới nào được index. Chỉ tăng coverage
sau khi source được xác minh và publish corpus/index có binding mới. Không nới
gold, đổi judge, xóa case khó hoặc dùng holdout đã xem để báo điểm xác nhận.

Khi người dùng quyết định chạy API: xác minh model/pricing theo tài khoản thật,
chọn cap cụ thể, DB evaluation riêng và checkout sạch; P7 yêu cầu các cờ
`--allow-network`, `--database-url`, `--run-budget-usd`. Chưa chạy bước này.
Không đưa key vào chat, source, log hoặc ZIP.

## Nguồn nghiên cứu sơ cấp

- [OpenAI — Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices):
  kết hợp eval tự động với đánh giá người thật và lặp lại theo lỗi quan sát được.
- [Anthropic — Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents):
  kiểm tra outcome, đọc transcript và duy trì harness đáng tin cậy.
- [Kapoor et al. — AI Agents That Matter](https://arxiv.org/abs/2407.01502):
  đo chi phí cùng độ chính xác, kiểm soát overfitting và tái lập đánh giá.
- [Debenedetti et al. — AgentDojo](https://arxiv.org/abs/2406.13352):
  cần đo utility và khả năng chống prompt injection trong môi trường tool có
  dữ liệu không đáng tin. Unit test offline không thay thế phép đo này.
