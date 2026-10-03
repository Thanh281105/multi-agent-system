# Challenge kỹ thuật từ review, phiên bản 1

Bộ này có 20 câu hỏi tiếng Việt mới và 5 probe cho evaluator. Nó được Codex
soạn từ catalog snapshot, review metadata và corpus ghi chú nguồn hiện có,
không lấy câu hỏi hay câu trả lời từ conversation gold hoặc output của SUT.
Đây là **engineering challenge newly prepared for locking**, không phải test
set do bên ngoài tạo hay dữ liệu đã được người thật chấm. Việc soạn có biết
các lỗi E1–E5 trong review; kết quả sau này chỉ là evidence regression cho
những lỗi đó, không thay thế P7/P8 hay chứng minh quality gain tổng quát.

`packet.json` chứa câu hỏi, oracle và source binding. `lock.json` đề xuất khóa
canonical hash của packet cùng raw hash các nguồn. Operator phải review và
ghi nhận hash này trong run manifest **trước** khi gửi câu đầu tiên tới SUT;
hiện operator lock và manual human audit đều `pending`. Không sửa packet sau
run để hợp câu trả lời. Nếu cần thay câu hỏi hoặc oracle, tạo version mới.
Không có prompt nào được chạy tới SUT khi tạo bộ này.

| Stratum | Số câu | Phạm vi |
| --- | ---: | --- |
| `typed_constraints` | 5 | Giá, rating, số trang; dấu phẩy thập phân, `k`, nghìn, đồng |
| `fresh_comparison` | 2 | Hai title trong conversation mới, có/không ngoặc kép |
| `natural_review` | 2 | Tên sách sau command tự nhiên, sample và population tách biệt |
| `book_domain` | 3 | Loại sữa khỏi sách, từ chối sữa/điện thoại ngoài phạm vi |
| `semantic_knowledge` | 4 | Diễn đạt tiếng Việt từ work-level source tiếng Anh |
| `unsupported_ambiguous` | 4 | Thiếu edition/ISBN, toàn văn, bộ nhiều tác phẩm, award support |

Các fact có `match_mode`, giá trị mong đợi và `authority`: raw source file,
record pointer (JSON array index bắt đầu 0), canonical record hash, value
pointer và exact source value. Span dùng Unicode code points, khoảng `[start,
end)`. Catalog span nằm trong raw JSONL record; knowledge span nằm trong
`content_markdown`. `catalog_row_number` và review `jsonl_line_numbers` bắt
đầu 1 và chỉ giúp mở source, không cung cấp product ID cho SUT.

`facts` là inventory oracle có thể dùng để kiểm claim; `required_fact_ids`
chỉ rõ fact mà câu trả lời cần thể hiện. Với refusal/abstention, đánh giá
`behavior` và `prohibited_behavior`; không buộc câu trả lời liệt kê mọi fact
trong inventory. Gợi ý sách có thể chọn subset hợp lệ từ allowed IDs, không
được coi record sữa trong inventory loại trừ là một fact phải giới thiệu.

Giá và rating là số trong snapshot, không phải giá thị trường hiện tại. Mẫu
review được tính từ số record/rating đã lưu, không coi `source_review_count`
là số mẫu và không suy luận sentiment accuracy, fraud hoặc toàn bộ người mua.
Không có raw review prose được sao chép vào packet. Semantic oracle chỉ có
phạm vi work của ghi chú nguồn, không nâng lên edition hay whole-book content.
Abstention ở đây nghĩa là chưa có authority trong corpus bị khóa, không có
nghĩa một sự kiện ngoài thế giới chắc chắn sai.

Năm `evaluator_probes` là candidate claim do Codex chủ động viết để kiểm tra
numeric equality, paraphrase, negation và claim vay citation cho award.
Đó không phải câu trả lời của SUT. Không tính probe vào 20 user requests hoặc
vào chất lượng runtime. Dấu hiệu `15.0/5` phải sai với rating 5; diễn đạt
tiếng Việt đúng nguồn không được yêu cầu chứa substring tiếng Anh.

Helper chỉ dùng Python stdlib, không import app, mở database, gọi provider,
hoặc chấm câu trả lời. Từ repository root:

```powershell
.venv/Scripts/python.exe evaluation/v3/review-challenge-v1/verify_packet.py
.venv/Scripts/python.exe evaluation/v3/review-challenge-v1/verify_packet.py --export-requests output/evaluation-v3/review-challenge-v1/requests.jsonl
```

Export dùng create-only và chỉ chứa `case_id`, `stratum`, `fresh_conversation`,
`message`, `packet_canonical_sha256`. Đây là handoff input cho runner/operator,
không tự dispatch. Mỗi request phải có conversation mới, cùng frozen runtime
binding và authorization hợp lệ. Không đưa oracle/fact/source ID vào message
hay model context. Lưu output/receipt/run manifest ở directory versioned mới;
run manifest phải bind packet hash, source/runtime/protocol pins, variant,
repeat count và identity của từng cell. Nếu adapter hiện hành chưa nhận
schema challenge này, dùng adapter rõ ràng và review nó trước khi chạy; không
coi JSONL export là P8-compatible gold hoặc run đã hoàn thành.

## Manual stratified human audit đang chờ

Trước khi dùng kết quả cho luận văn, reviewer thật đọc ít nhất 12/20 câu
(2 câu mỗi stratum: `c01,c05,c06,c07,c08,c09,c10,c12,c13,c16,c17,c20`,
đã chọn trước khi có output) và cả 5 probe. Có thể mở rộng lên toàn bộ 20 câu.
Với mỗi câu, mở source record/span và xác nhận prompt rõ, oracle đúng field,
work/edition scope, hành vi answer/clarify/abstain phù hợp. Người audit chưa
xem answer/variant của SUT khi audit oracle. Sau đó một lượt audit answer
blinded riêng kiểm tra từng claim với chính citation của claim đó và ghi
verdict/uncertainty/rationale; không tự động suy từ presence của fact đúng.

Ghi artifact mới chứa reviewer IDs, phương pháp `human_review`, thời điểm,
packet hash và case IDs được audit, label/rationale cho từng mục, các source
hash đã mở và disagreement resolution. Giữ automated Codex authorship riêng
với human labels. Nếu sửa oracle sau audit, tạo packet version mới và khóa
trước run; nếu label bất định, báo uncertainty thay vì ép pass. Tới khi có
artifact audit đó, mọi human validation vẫn phải báo `pending`.
