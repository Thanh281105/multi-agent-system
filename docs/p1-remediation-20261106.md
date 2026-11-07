# Bản sửa P1 — 2026-11-06

P1 nối tiếp P0, tập trung vào tính đúng của bộ kiểm chứng, đường chạy được hỗ
trợ và lỗi vận hành đã tái hiện. Không tạo kết quả thí nghiệm hoặc phiếu người
chấm mới. Bản ZIP ban đầu và dữ liệu frozen được giữ nguyên để truy vết.

## Thay đổi có thể kiểm chứng

1. **Report phải khớp analysis.** Tính lại toàn bộ report từ analysis khi ghi và
   đọc. Thay số, run ID hoặc số observation rồi tính lại hash vẫn bị từ chối.
   Complete bundle yêu cầu đúng một judgment cuối cùng cho mỗi answer.
2. **Tính lại phép tổng hợp.** Kiểm tra delta, trung bình, median, win/tie/loss,
   group ID duy nhất và bootstrap với đúng seed/số mẫu đã khai báo. Với metric
   có trong judgment, nối lại repetition → conversation → work group và đối
   chiếu giá trị/count. Export ghi riêng phạm vi reconciliation cho từng cặp.
   Chi phí không có trong judgment chưa được tính lại từ execution receipts.
3. **Tách đọc lịch sử khỏi cho phép thực thi.** Giữ được model/reasoning cũ khi
   đọc protocol, không sửa frozen JSON. Preflight live từ chối model/reasoning
   không được composer hiện tại hỗ trợ và judge dùng chung model generation,
   trước khi khởi tạo account ngân sách hay provider.
4. **Ngừng runner ad-hoc không an toàn.** 15 script cũ từng sửa DB/corpus,
   checkpoint, attribution hoặc threshold giờ chỉ trả exit 2 với hướng dẫn.
   Lưu byte nguyên bản và manifest tại `docs/legacy-scripts/`; không xóa lịch sử.
5. **Sửa migration/readiness.** Revision mà readiness yêu cầu khớp Alembic head
   `20261013_0009`. Migration timeout dùng batch ALTER để hỗ trợ SQLite; downgrade
   kiểm tra dữ liệu trước DDL, từ chối khi có timeout trên 18 giây. Regression
   lưu attempt thật trong SQLite/PostgreSQL và kiểm tra không mất/sửa bản ghi.
6. **Sửa hợp đồng type và fixture bị lỗi thời.** Type-check toàn bộ `app`, sửa
   xử lý đơn vị so sánh không hợp lệ, giữ non-null runtime trong callback async,
   chuẩn hóa dynamic Pydantic type và error code. Fixture SHA Git dùng 40 ký tự,
   timeout SUT 30/90 giây, timeout judge riêng 18 giây, reasoning mặc định `none`.
   Cancellation test tạo đủ subject để thật sự đi vào expert stage; no-RAG test
   dùng model-call evidence đúng schema. Không nới giới hạn production để ép test.
7. **Sửa hành vi public runtime đã tái hiện.** Prefix "Giúp mình xem review của…"
   được tách đúng tên sách; câu hỏi "Sách này… Không dùng review…" giữ nghĩa vụ
   knowledge. Proposal trong required mode bị từ chối nếu model đổi số candidate
   đã được cấp quyền. Retry đọc lại trạng thái DB khi claimant đã xóa active task,
   không trả snapshot `running` cũ sau khi turn đã hoàn tất; terminal replay chỉ
   phát một sự kiện kết thúc. Regression điều khiển cửa sổ race và kiểm tra một
   provider attempt duy nhất, kèm HTTP JSON/SSE/cancel.

Ruff sửa import/format trên phạm vi mà CI kiểm tra. Việc tách string dài không
được phép đổi giá trị prompt. Không thay corpus, frozen metrics hoặc model pin.

## Kiểm chứng và cách chạy lại

Cài Python 3.12 và dependency từ `requirements-dev.lock`. Dùng PostgreSQL 16
thử nghiệm riêng: các test tự tạo/drop database tên ngẫu nhiên dưới prefix
`thanh_v2_p2_`, không dùng database production. Không bỏ qua PostgreSQL test khi
thiếu server. Thiết lập `TEST_POSTGRES_URL` tới server thử nghiệm trước khi chạy.

```powershell
ruff check app tests migrations scripts
ruff format --check app tests migrations scripts
mypy app
pytest -m "not integration" -q
```

Trong `frontend`: `npm ci`, `npm run lint`, `npm test`, `npm run build`.
Build wheel sau frontend build và kiểm tra assets đã được đóng gói.
Chỉ chạy provider integration khi đã cấu hình khóa, hạn mức và cho phép mạng.
Suite có ca dựng đủ 720 observation; runtime lâu hơn các unit test nhỏ.

Snapshot ZIP không chứa `.git`. Trước khi tạo binding/lần chạy live mới, khởi
tạo repository và commit source hoặc dùng checkout có commit xác định. SHA Git
không thể suy ra từ tên ZIP; không đặt revision giả để vượt preflight.

## Giới hạn giữ hiển thị

- P8 gốc thiếu `preparation.json`; không suy diễn hoặc tạo giả file này.
- Phục hồi CRLF chính xác ba file catalog chỉ khôi phục byte, không chứng nhận
  thời điểm freeze hoặc tính độc lập của tập.
- Chưa có phiếu người chấm thật được cung cấp; không tuyên bố judge đã hiệu chuẩn.
- Chưa chạy benchmark provider mới, load/soak, Qdrant/Redis thực hoặc triển khai
  Kubernetes. Mock HTTP và regression PostgreSQL không thay các kiểm chứng đó.
- Corpus 20 sources và mapping 20/200 sản phẩm vẫn là giới hạn hiện tại.
  Document recall chưa đo thì giữ null. Không tự sinh relevant-document gold.
- Chưa có bằng chứng mới để đổi routing, tăng claim cap, thêm reranker hoặc chạy
  tool song song. Giữ baseline và budget chung; đo trên development trước,
  đánh giá cuối bằng tập xác nhận mới chưa dùng để sửa hệ thống.

## Cơ sở thiết kế

Các khuyến nghị này được áp dụng theo phạm vi bằng chứng, không phải sự chứng
nhận của tác giả nghiên cứu cho dự án:

- [Anthropic — Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents):
  bắt đầu đơn giản và chỉ tăng độ phức tạp khi có nhu cầu đo được.
- [AI Agents That Matter](https://arxiv.org/abs/2407.01502): đánh giá chất lượng,
  tài nguyên, holdout và khả năng tái lập thay vì chỉ báo một điểm thành công.
- [MAST](https://arxiv.org/abs/2503.13657): phân loại lỗi phối hợp để sửa lỗi đã
  quan sát; không suy ra rằng thêm agent sẽ giải quyết thiếu evidence.
- [AgentDojo](https://arxiv.org/abs/2406.13352): kiểm tra tác vụ và hành vi agent
  trước dữ liệu tool không đáng tin; không coi lời từ chối là bằng chứng duy
  nhất cho authorization server-side.

Kết quả kiểm tra thực tế của gói bàn giao nằm trong báo cáo P1 và các log/JUnit
đi kèm. Test xanh chứng minh các contract đã kiểm tra, không bảo đảm hệ thống
đúng trên mọi đầu vào hoặc chứng minh MA tốt hơn SA.
