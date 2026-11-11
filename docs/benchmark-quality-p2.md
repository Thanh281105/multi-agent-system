# Cải thiện chất lượng runtime để chuẩn bị benchmark — P2

Ngày đối chiếu tài liệu: 2026-11-06. Phạm vi là runtime public dùng chung cho
các nhánh benchmark. P2 bao gồm P0/P1; không đổi judge, gold, metric, model pin,
giới hạn gọi generation, input/output hoặc ngân sách để làm điểm đẹp hơn.

## Nghiên cứu và quyết định triển khai

| Nguồn gốc | Nguyên tắc có liên quan | Áp dụng vào dự án |
|---|---|---|
| [OpenAI — Evaluate agent workflows](https://developers.openai.com/api/docs/guides/agent-evals) | Tìm lỗi ở trace, rồi kiểm tra lại bằng các trường hợp lặp được. | Tái hiện điểm mất query/evidence/field trước khi sửa; lưu probe, source hash và JUnit. |
| [Daniel Ford / Anthropic — Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval) | Chunk mất ngữ cảnh có thể làm retrieval bỏ sót nguồn; kết hợp lexical và dense có ích tùy dữ liệu. | BM25 và lexical coverage sử dụng tiêu đề đã có trong chunk cùng phần thân. Đây là metadata context, không phải triển khai toàn bộ contextual embeddings hay reranker của bài viết. |
| [Anthropic — Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) | Ưu tiên thành phần đơn giản, chỉ thêm độ phức tạp khi có nhu cầu. | Sửa việc mất dữ kiện trước; không thêm agent hoặc lượt LLM reranking cho các lỗi xử lý dữ liệu. |
| [Kim và cộng sự — Towards a Science of Scaling Agent Systems, v3](https://arxiv.org/abs/2512.08296v3) | Lợi ích phối hợp phụ thuộc loại tác vụ và chi phí điều phối. | Không giả định MA phải thắng; thay đổi chung cho các nhánh, giữ model/budget để đo tác động. |
| [Cemri và cộng sự — MAST](https://arxiv.org/abs/2503.13657v3) | Phân loại lỗi thiết kế, truyền thông tin và xác minh. | Đối chiếu các chỗ mất thông tin giữa planner, retriever, context và câu trả lời. |
| [Kapoor, Stroebl, Narayanan và cộng sự — AI Agents That Matter](https://arxiv.org/abs/2407.01502) | Cần đo cả chất lượng, chi phí, holdout và khả năng tái lập. | Không gọi probe tổng hợp là benchmark; giữ source/budget bindings, yêu cầu đánh giá mới trước kết luận chất lượng. |

Đây là suy luận thiết kế từ tài liệu và lỗi đã tái hiện trong code, không phải
sự chứng nhận của các tác giả cho dự án. Không chuyển các mức cải thiện trong
nghiên cứu khác thành dự báo cho corpus sách này.

## Các lỗi đã tái hiện và sửa

1. **Query rewrite làm mất tên/ID gốc.** Model rewrite chỉ bổ sung vào truy vấn
   gốc, vẫn tối đa bốn truy vấn và không được mở rộng ACL/source allowlist.
2. **Nguồn chỉ có tên sách ở tiêu đề bị bỏ sót lexical.** Chấm tiêu đề + thân
   chunk bằng dữ liệu đã cấp quyền. Nội dung citation, vị trí span và hash không
   đổi. Metadata giúp tìm nguồn; nó không tự chứng minh khẳng định về nội dung.
3. **Tiếng Việt NFC/NFD bị tokenize khác nhau.** Chuẩn hóa NFC cho lexical;
   không bỏ dấu hoặc gộp các tên có nghĩa khác nhau.
4. **Dedup theo content hash xóa mất nguồn khác.** Dedup theo source ID, source
   version và content hash. Hai nguồn khác nhau giữ attribution riêng; cùng
   phiên bản nguồn có chunk trùng vẫn được khử trùng lặp.
5. **Giới hạn 350 ký tự làm mất chi tiết cuối nguồn.** Context packer thử giữ
   tối đa 1.000 ký tự, hạ xuống mức ngắn hơn khi cần. Luôn tính cả instructions,
   JSON envelope và structured schema trong input bound cũ; không loại evidence
   bắt buộc để nhường chỗ cho phần tùy chọn. Nguồn dài hơn mức này vẫn có thể cần
   retrieval/chunking phù hợp; P2 không tuyên bố giải quyết mọi nguồn dài.
6. **Có evidence loại catalog chưa đồng nghĩa trả lời đủ.** Yêu cầu tên sách
   cho từng candidate, cùng giá/rating/số trang nếu planner đã dùng các điều
   kiện đó. Model chọn thiếu field phải qua đường sửa/fallback đã kiểm chứng,
   thay vì được coi là đủ chỉ vì có một claim giá được nguồn hỗ trợ.

Context có thể dài hơn, và giữ query gốc có thể tăng số query embedding trong
batch. Không thêm tầng model mới; không tuyên bố latency hoặc chi phí giảm
trước khi đo. Giới hạn bốn query, context retrieval 6.000 token và các budget
generation/attempt được giữ nguyên.

## Đối chứng development, không phải điểm benchmark

`tests/test_runtime_quality.py` chứa tám probe tổng hợp do đợt sửa này tạo:
retention query, title retrieval, attribution, excerpt muộn, field completeness,
Unicode, dedup trong nguồn và input budget. Chúng là regression/development;
không phải một tập xác nhận độc lập và không đo accuracy của model thật.

Đối chứng cùng probe trên P1: **6 failed, 2 passed**. Trên P2: **8 passed**.
Các kết quả này chứng minh những cơ chế cụ thể đã sửa, không có nghĩa benchmark
held-out sẽ đạt 100% hoặc MA sẽ vượt SA.

Chạy lại bằng môi trường dev có pytest và pytest-asyncio:

```powershell
python scripts/run_runtime_quality_probes.py `
  --baseline-source C:/PATH/TO/P1_SOURCE `
  --output output/runtime-quality/NEW_COMPARISON
```

Output phải là thư mục mới. Script khẳng định đúng app source được nạp, dùng
cùng file probe, ghi hash và giữ riêng log/JUnit mỗi nhánh. Chỉ chạy probe
tổng hợp; không gọi provider hoặc nối database production. Có thể bỏ
`--baseline-source` để kiểm tra riêng source hiện tại.

## Kiểm tra dữ liệu thật và CI

Test mới trong `test_v2_catalog_regressions.py` đi qua planner → catalog tools →
assessment → grounded renderer trên snapshot nhập thật, ở SQLite và PostgreSQL.
Nó yêu cầu hai candidate có đủ tên, giá, rating và số trang, không truyền gold.

Marker `integration` trong repo còn chứa SQL/catalog test không gọi provider.
Vì vậy CI P2 loại riêng file OpenAI live test, chạy cả các kiểm tra SQL này:

```powershell
ruff check app tests migrations scripts
ruff format --check app tests migrations scripts
mypy app
# TEST_POSTGRES_URL phải chỉ tới PostgreSQL thử nghiệm riêng.
pytest --ignore=tests/test_agent_integration.py -q
```

Chưa có kết quả remote CI chỉ vì kiểm tra cục bộ qua. Kết quả test/đóng gói
thực tế của bản bàn giao nằm trong báo cáo P2 đi kèm.

## Chạy benchmark tiếp theo

Commit source mới trước khi tạo binding. Dùng CLI P7/P8 được duy trì; model và
reasoning phải khớp composer. Tạo run/binding mới, không ghi đè protocol hoặc
artifact frozen. Có thể kiểm tra đầu vào cấu hình mini hiện có bằng lệnh offline:

```powershell
python -m app.evaluation.v3_cli validate `
  --experiment evaluation/v3/mini-generation-luna-judge-v1/experiment.v3.json
```

Lệnh validate không chạy model và không đo chất lượng. Trước live benchmark cần
database có đúng snapshot, quyền mạng/khóa và hạn mức rõ ràng theo CLI. Chạy
development/pilot trước, xem lỗi và chi phí, rồi khóa source/prompt/protocol.
Đánh giá cuối bằng tập mới chưa dùng để sửa hệ thống; báo cả SA và MA, số liệu
thua, độ đầy đủ, citation support, độ trễ và chi phí. Human calibration phải có
phiếu thật; không thay bằng nhãn mô phỏng hoặc nới threshold.

Corpus 20 nguồn và mapping 20/200 vẫn là giới hạn. P2 không bịa nội dung cho
180 sản phẩm còn lại, không hạ điều kiện abstention hay tự tạo document recall.
Chất lượng live, calibration, lợi ích reranker/concurrency và kết luận MA hơn
SA vẫn cần phép đo riêng. Không có bảo đảm benchmark sẽ "xuất sắc" chỉ từ test.
