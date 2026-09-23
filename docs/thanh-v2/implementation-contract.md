# Kế hoạch `thanh-v2`: hoàn thiện chatbot Multi-Agent + RAG trong repo hiện tại

## 1. Chốt phạm vi, nhánh và nguyên tắc tích hợp

Triển khai trực tiếp trong [Multi-Agent](C:/Users/Admin/Desktop/DA_CNTT/Multi-Agent), trên nhánh mới **`thanh-v2`**, bắt đầu từ `thanh-v1` tại commit `b7df1cc`. Nhánh `thanh-v2` hiện chưa tồn tại và working tree đang sạch.

Lệnh tạo nhánh khi bắt đầu triển khai:

```powershell
git switch -c thanh-v2 thanh-v1
```

Giữ toàn bộ lịch sử hiện có của `thanh-v1`. Commit Ali Shazal không nằm trong ancestry của nhánh này; tạo `thanh-v2` từ đây sẽ không mang commit đó vào lịch sử.

**Cách lấy phần tốt từ `lac-v2`:**

- Port chọn lọc RAG, hợp đồng công cụ, provenance và cơ chế đề xuất/duyệt thay đổi.
- Ghi commit nguồn `94af718` của `tgialac` và nguồn gốc từng phần được sử dụng.
- Không merge toàn bộ nhánh `lac-v2`, không chép nguyên bảy package hoặc đưa thêm runtime Claude vào.
- Giữ copyright và giấy phép liên quan; phần được sửa có ghi nhận thay đổi theo [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0).
- Giữ nguyên folder `Multi-Agent-lac-v2`, bao gồm thay đổi `.gitignore` đang có.

**Sản phẩm hoàn thành trong đợt này:**

Chatbot tiếng Việt có hai chế độ shopper/merchant, điều phối chuyên gia theo yêu cầu, RAG từ nguồn internet phù hợp với sách hiện có, bộ nhớ có kiểm soát, thao tác sandbox trong hội thoại và thực nghiệm so sánh tái lập được.

Web là giao diện hội thoại và bằng chứng thực thi. Không xây storefront, hệ thống CRUD quản trị, đăng ký tài khoản hay thanh toán thật. **Deploy VPS, domain, TLS và vận hành công khai để sau.**

“Làm một lần xong” được tổ chức thành một đợt triển khai liên tục có các mốc kiểm tra nội bộ; không cần xin duyệt lại từng module. Hoàn thành được xác định bằng checklist nghiệm thu ở cuối, không chỉ bằng việc chạy được một câu hỏi demo.

## 2. Kiến trúc và hợp đồng cần chốt trước khi viết chức năng

### Một runtime chung, hai chiến lược sử dụng

Giữ Python 3.12, FastAPI, OpenAI Responses, PostgreSQL, Redis và React hiện có. Không chuyển sang framework agent khác.

| Thành phần | Trách nhiệm |
|---|---|
| Supervisor | Hiểu yêu cầu, chọn kế hoạch, đánh giá thiếu evidence, quyết định bước bổ sung |
| Product Agent | Tìm, lọc, so sánh và xếp hạng sách |
| Review Agent | Truy xuất và phân tích review trong snapshot |
| Trust Agent | Tín hiệu phàn nàn/chất lượng văn bản, giữ giới hạn heuristic hiện có |
| Market Agent | Thống kê cắt ngang snapshot, không suy ra xu hướng thị trường |
| Knowledge Agent | Truy hồi tài liệu về sách/tác giả và trả evidence có phiên bản |
| Merchant Agent | Phân tích dữ liệu cửa hàng demo và tạo đề xuất thay đổi |
| Action service | Xác thực và thực thi thao tác sandbox bằng transaction |

Shopping là một chế độ của chatbot chung, không có supervisor thứ hai. Merchant cũng sử dụng cùng hệ thống quyền, công cụ, evidence và ngân sách.

### API v2 là versioned API duy nhất

Client sử dụng schema nghiêm ngặt; API `/api/v2` là contract versioned duy nhất:

- Các endpoint `/api/v1` đã bị gỡ; `/api/v2` phục vụ chatbot hợp nhất.
- `POST /chat` là fixture Phase 1 riêng, không phải alias hoặc phiên bản API.
- Historical evaluation v1/v2 artifacts được giữ nguyên để tái lập kết quả.

| API v2 | Chức năng |
|---|---|
| `GET /me` | Trả danh tính đã xác thực và các chế độ được phép |
| `POST /conversations`, `GET /conversations` | Tạo/liệt kê hội thoại của người dùng |
| `GET/DELETE /conversations/{id}` | Đọc/xóa hội thoại thuộc quyền sở hữu |
| `POST /chat`, `POST /chat/stream` | Chạy một lượt JSON hoặc SSE |
| `GET /turns/{id}`, `POST /turns/{id}/cancel` | Đọc trạng thái/kết quả hoặc yêu cầu hủy |
| `GET /actions/{id}` | Đọc đề xuất và kết quả thực thi |
| `POST /actions/{id}/confirm`, `/reject` | Xác nhận/từ chối đề xuất |
| `GET/PUT/DELETE /memory` | Quản lý sở thích được ghi nhớ rõ ràng |

Request chat gồm `conversation_id`, `client_turn_id` và `message`; giới hạn message giữ ở 2.000 ký tự. `client_turn_id` do client tạo và giữ nguyên khi retry.

Response v2 chứa:

- `conversation_id`, `turn_id`, request/trace IDs.
- Trạng thái thực thi và kết quả hội thoại.
- Câu trả lời, claims, citations và action cards.
- Các phiên bản kế hoạch, bước thực sự đã chạy và kết quả tái sử dụng.
- Usage, chi phí ước tính, fallback và cảnh báo phù hợp.

Giữ `TaskStatus` cho thực thi. Thêm `DialogueOutcome` riêng:

```text
answered
needs_clarification
awaiting_confirmation
abstained
```

Hỏi lại, chờ xác nhận và từ chối vì thiếu bằng chứng là kết quả hội thoại hợp lệ, không chuyển thành HTTP 503.

### Danh tính và lưu trữ

- Mỗi hội thoại gắn cố định với tenant, principal, chế độ và cửa hàng demo.
- Đổi shopper/merchant sẽ mở hoặc chọn hội thoại khác; không chuyển quyền ngay trong một transcript.
- Mọi request đọc history, memory, citation và action đều kiểm tra quyền hiện tại.
- PostgreSQL là nguồn lưu trữ bền vững cho v2. Redis hỗ trợ coordination/cache, không quyết định một thao tác đã được thực hiện hay chưa.
- Ghi nhận lượt chat trước khi gọi model; kết quả cuối được lưu trước khi gửi terminal event.

Các nhóm migration bổ sung sau revision hiện tại:

| Nhóm | Dữ liệu |
|---|---|
| Hội thoại | Conversations, turns, kết quả từng bước, preferences |
| Sandbox | Offers, carts, orders, proposals, idempotency và audit |
| Knowledge | Documents/chunks có phiên bản, mapping với sách, corpus/index manifest |
| Ngân sách | Reservation và kết quả từng lần gọi provider |

Migration chỉ thay schema. Seed demo là lệnh riêng, chạy lại không ghi đè thao tác người dùng đã thực hiện.

## 3. Chi tiết triển khai các chức năng cốt lõi

### A. Supervisor thích nghi

Luồng chính:

```text
Nhận lượt chat
→ khôi phục ngữ cảnh được phép
→ xác định yêu cầu/ràng buộc
→ chọn và biên dịch kế hoạch
→ chạy chuyên gia
→ đánh giá evidence
→ bổ sung bước đọc nếu cần
→ tạo và kiểm chứng câu trả lời
→ lưu kết quả
→ trả lời
```

**Routing và planning**

- Model chọn intent/template trong danh sách được registry cho phép theo chế độ.
- Python xác thực giá, số lượng, tên sách, entity và dependency; ID sách phải được giải quyết từ catalog hoặc ngữ cảnh đã có.
- Yêu cầu rõ ràng của người dùng trở thành nghĩa vụ evidence, ví dụ cần so sánh giá, xem review hoặc giải thích chủ đề.
- Model không được bỏ nghĩa vụ đó chỉ để có kế hoạch ngắn hơn.
- Các template bao gồm: catalog, so sánh, recommendation có review/trust, knowledge, catalog kết hợp knowledge và merchant read/proposal.

**Điểm đánh giá evidence**

Sau kế hoạch đầu, Python tạo `EvidenceAssessment` gồm:

- Yêu cầu nào đã được đáp ứng.
- Bằng chứng còn thiếu hoặc mâu thuẫn.
- Sách ứng viên thực sự tìm thấy.
- Lỗi công cụ và khả năng thử lại.
- Ngân sách/thời gian còn lại.

Supervisor chọn kết thúc, hỏi lại, từ chối hoặc chạy continuation được phép.

**Giới hạn bắt buộc**

| Giới hạn | Giá trị ban đầu |
|---|---:|
| Điều chỉnh kế hoạch | Tối đa 1 lần/lượt |
| Bước đọc bổ sung | Tối đa 2 |
| Tổng bước chuyên gia | Tối đa 8 |
| Sách ứng viên | Tối đa 5 |
| Lượt truy hồi knowledge | Tối đa 2 |
| Sửa bản nháp câu trả lời | Tối đa 1, nếu còn ngân sách |

Mỗi bước có operation key từ capability, tham số đã xác thực và phiên bản dữ liệu. Continuation chỉ chạy bước mới, tái sử dụng kết quả hợp lệ; không gọi lại toàn bộ executor trên DAG cũ.

Giữ bốn mode:

- `off`: quyết định deterministic, không gọi model sinh văn bản.
- `shadow`: ghi lựa chọn model nhưng thực thi chính sách deterministic.
- `hybrid`: dùng lựa chọn model hợp lệ, fallback khi lỗi.
- `required`: lỗi model được ghi nhận rõ; không âm thầm biến thành kết quả deterministic.

### B. Corpus internet và RAG

**Thu thập và đối chiếu nguồn**

1. Lập manifest cho toàn bộ 200 sách hiện có.
2. Đối chiếu tên, tác giả, nhà xuất bản, thông tin ấn bản và ISBN khi có.
3. Phân loại mapping: đúng ấn bản, đúng tác phẩm, còn mơ hồ hoặc không tìm được.
4. Nguồn mơ hồ không được tự động xuất bản vào corpus.
5. Hoàn thành corpus ban đầu cho ít nhất 20 tác phẩm; mở rộng theo nguồn tìm được và báo cáo tỷ lệ bao phủ.

Nguồn khởi đầu đã xác định gồm [Sapiens trên trang tác giả](https://www.ynharari.com/book/sapiens/) và [Dế Mèn minh họa Đậu Đũa của Kim Đồng](https://nxbkimdong.com.vn/products/de-men-phieu-luu-ky-2). Có thể bổ sung metadata từ Wikidata và nguồn thư mục phù hợp.

Mỗi tài liệu lưu URL, cơ sở sử dụng, thời điểm thu thập, content hash, phiên bản và quan hệ với tác phẩm/ấn bản. Chỉ nhập nguyên văn khi quyền sử dụng cho phép; trường hợp khác dùng metadata và ghi chú sự kiện có dẫn nguồn.

Không lấy toàn văn sách không rõ quyền. Không đưa câu hỏi/đáp án benchmark vào corpus. Các chính sách sandbox tự biên soạn phải được gắn nhãn demo.

**Pipeline**

```text
Nguồn đã duyệt
→ làm sạch
→ đối chiếu sách
→ chia chunk
→ embedding
→ xuất bản corpus version
→ lọc quyền
→ BM25 + dense retrieval
→ hợp nhất bằng RRF
→ kiểm tra liên quan
→ EvidenceBundle
```

- Port thuật toán và test có ích từ `lac-v2`.
- Dùng `ModelRuntime` hiện tại cho rewrite/verifier; không đem sang provider loop thứ hai.
- Embedding mặc định theo cấu hình hiện có: `text-embedding-3-small`, 1.536 chiều.
- Hash embedding chỉ phục vụ offline regression.
- Lưu document/chunk/vector trong PostgreSQL; xếp hạng trên corpus nhỏ đã lọc quyền.
- Qdrant tiếp tục tắt trong đợt này.
- Trả mặc định sáu chunk, tối đa tám sau mở rộng ngữ cảnh; tổng context retrieval không quá 6.000 token.
- Giới hạn chunk áp dụng cả với bảng; bảng dài được tách theo hàng và lặp header.

Fingerprint index gồm embedding model, dimension, chunker và enrichment policy. Thay fingerprint phải xây index mới; không tái sử dụng vector cũ chỉ vì nội dung tài liệu chưa đổi.

Một lượt chat sử dụng một corpus version cố định. Quyền truy cập được lấy từ server; model chỉ có thể thu hẹp phạm vi truy hồi.

Ngưỡng “đủ liên quan” được chọn trên development set theo khả năng phân biệt câu có/không có đáp án, rồi đóng băng trước benchmark. Không ép trả tài liệu khi điểm thấp.

**Citation và grounding**

- Evidence ID ổn định theo nguồn, phiên bản và chunk; `[C1]` chỉ là nhãn hiển thị.
- Claim dẫn tới đúng evidence/span, không gắn mọi nguồn vào mọi câu.
- Facts có cấu trúc như giá, số trang và số lượng lấy từ công cụ, không để model tự điền.
- Kiểm tra citation, entity, số liệu và khả năng hỗ trợ nội dung trước khi phát câu trả lời.
- Dùng semantic verifier cho claims dựa trên tài liệu; lexical overlap chỉ là một kiểm tra phụ.
- Nếu sửa một lần vẫn không đạt, trả facts/trích đoạn đã kiểm tra hoặc nêu thiếu bằng chứng.
- Tách rõ dữ liệu lịch sử, nội dung tài liệu ngoài và dữ liệu sandbox.

### C. Shopping và merchant trong chat

**Dữ liệu sandbox**

Tạo offers riêng cho sách trong snapshot:

- Giá khởi tạo từ snapshot nhưng được ghi rõ là giá demo.
- Tồn kho khởi tạo deterministic, mặc định 10 cuốn/offer.
- Merchant chỉnh offer, không sửa bảng sản phẩm/review lịch sử.
- Shopper trong cửa hàng demo lọc ngân sách theo giá offer; rating/review vẫn mang nhãn snapshot.
- Fixture `POST /chat`, khi được bật trong development, giữ semantics Phase 1 lịch sử.

**Các luồng bắt buộc**

| Vai trò | Luồng |
|---|---|
| Shopper | Tìm sách theo ràng buộc → so sánh → hỏi thêm nội dung/review |
| Shopper | Thêm/sửa/xóa giỏ hàng → xem tổng tiền → xác nhận đơn sandbox |
| Merchant | Hỏi catalog/tồn kho demo → giải thích bằng evidence |
| Merchant | Đề xuất đổi giá/điều chỉnh tồn kho → xem trước → duyệt/từ chối |

Không bổ sung campaign, phân tích quảng cáo hoặc tích hợp thương mại thật.

**Thao tác ghi**

- Model tạo đề xuất; action service sở hữu quyền thực thi.
- Checkout và thay đổi merchant bắt buộc có card xác nhận.
- Card chứa proposal ID/version, đối tượng, giá trị trước/sau, phiên bản dữ liệu và hạn xác nhận 10 phút.
- “Đồng ý” trong lời model không phải approval.
- Confirmation có `Idempotency-Key`; cùng key/cùng payload trả kết quả cũ, cùng key/khác payload trả conflict.
- Một transaction ngắn kiểm tra quyền, khóa đối tượng, kiểm tra version/tồn kho, thực hiện thay đổi và ghi audit.
- Không gọi model hoặc network trong transaction.
- Giá/tồn kho/cart thay đổi sau preview làm proposal hết hiệu lực; cần preview mới.
- Checkout tạo order bất biến, trừ tồn kho và tiêu thụ cart version đúng một lần.

Hủy chat không hoàn tác một action đã commit. Nếu client mất kết nối sau xác nhận, nó đọc lại kết quả theo action ID/key thay vì tự thực hiện lần nữa.

### D. Hội thoại, memory và SSE

- Lưu transcript và kết quả cuối ở PostgreSQL; không phụ thuộc vào TTL Redis để khôi phục history.
- Context cho model gồm lượt gần đây, ràng buộc đang hiệu lực, sách đang được nhắc tới và preference liên quan.
- Chỉ ghi memory khi người dùng yêu cầu rõ ràng; lưu nguồn từ lượt nói.
- Memory giới hạn ở sở thích sách/ngôn ngữ/ngân sách, không suy diễn đặc điểm nhạy cảm từ lịch sử đọc.
- Yêu cầu hiện tại ưu tiên hơn memory cũ.
- Đăng xuất/đổi credential xóa context và cache giao diện; chỉ tải history sau xác thực.
- Xóa hội thoại đồng thời vô hiệu proposal còn chờ và xóa memory phát sinh từ hội thoại đó.

SSE phát status thật theo bước và revision; text chỉ phát sau grounding. Mỗi stream có đúng một terminal event.

Khi disconnect/timeout: dừng dispatch mới, ghi `cancelled` hoặc `interrupted`, giữ kết quả đọc đã có. Retry cùng `client_turn_id` không chạy model lần nữa; client nhận trạng thái/kết quả đã lưu. V1 không bị thay đổi theo cơ chế này.

### E. Ngân sách và telemetry

Thêm ledger dùng chung cho chat, ingestion, benchmark, warmup, embedding và judge.

Trước mỗi lần gửi request provider, kể cả retry:

1. Xác nhận model và pricing manifest.
2. Kiểm tra payload/output limit.
3. Reserve chi phí ước tính theo input và output tối đa.
4. Chỉ gọi nếu còn ngân sách.
5. Ghi usage trước khi kiểm tra kết quả parse.
6. Timeout hoặc thiếu usage giữ reservation ở trạng thái chưa xác định, không tính bằng 0.

Giới hạn ban đầu cho v2:

| Chính sách | Mặc định |
|---|---:|
| Cảnh báo toàn đợt | 50 USD |
| Trần reservation toàn đợt | 100 USD |
| Trần một lượt chat | 0,25 USD |
| Lần gọi generation logic | Tối đa 10 |
| Provider attempts, gồm query embedding/retry | Tối đa 16 |
| Provider concurrency | 2 |
| Retry | Tối đa 1, còn thời gian/ngân sách |
| Timeout một attempt | 18 giây |
| Deadline toàn lượt | 60 giây |
| Input một generation call | Tối đa 12.000 token |
| Output một generation call | Tối đa 1.200 token |

Đây là giới hạn do ứng dụng tính và reserve; usage chưa xác định phải hiện riêng. Các giá trị có thể được hiệu chỉnh sau pilot, nhưng phải đóng băng trước benchmark chính thức.

### F. Giao diện chatbot

Giữ thiết kế Evidence Atlas hiện có và mở rộng theo `DESIGN.md`, UI UX Pro Max, Impeccable và shadcn.

- Trung tâm là transcript và composer.
- Sidebar quản lý hội thoại và chọn chế độ được cấp quyền.
- Evidence panel hiển thị claims, nguồn, agent execution và plan revisions.
- Product comparison, cart, order và proposal xuất hiện thành artifacts trong chat.
- Roster và DAG lấy từ kết quả thực thi, không tiếp tục hardcode chỉ bốn agent.
- History lưu liên kết tới artifacts/citations để mở lại, thay vì chỉ giữ text.
- Phân biệt rõ answered, cần hỏi lại, thiếu nguồn và chờ xác nhận.
- Có loading, partial, empty, offline, cancelled, expired và conflict states.
- Keyboard, focus, screen reader, reduced motion và layout 375/768/1024/1440px được kiểm tra.
- Không hiển thị chain-of-thought, raw prompt, secret hoặc raw tool payload.

## 4. Trình tự triển khai và thực nghiệm

### Các gói công việc thực hiện liên tục

| Thứ tự | Công việc | Điều kiện chuyển bước |
|---|---|---|
| 1 | Tạo `thanh-v2`, dựng môi trường, chạy baseline, ghi nguồn tích hợp | Xác định được trạng thái baseline và các lỗi môi trường |
| 2 | Contract v2, registry riêng, migrations, authorization, ngân sách | Contract/migration/auth/budget tests đạt |
| 3 | Corpus pipeline, mapping sách, retrieval và citation | Có corpus version hợp lệ; truy hồi/ACL/no-answer tests đạt |
| 4 | Supervisor, continuation, deduplication, grounding | Luồng đọc xuyên suốt chạy được và không vượt giới hạn |
| 5 | History, memory, sandbox shopping/merchant | Restart/retry/confirmation/concurrency tests đạt |
| 6 | API v2 và giao diện chat/artifacts | JSON/SSE/UI thống nhất, v1 regression đạt |
| 7 | Evaluation v3, gold set, pilot, hiệu chỉnh và đóng băng | Xác nhận đủ ngân sách và protocol không còn thay đổi |
| 8 | Benchmark chính thức, phân tích lỗi, kiểm thử cuối và tài liệu | Đủ checklist nghiệm thu |

Mỗi gói có commit riêng sau kiểm tra cần thiết. Không amend lịch sử cũ, không force-push và không tự tạo PR.

### Evaluation v3

Giữ nguyên schema, reader và captured results v1/v2. Thêm schema/runner `3.0`, tái sử dụng các hàm pairing/bootstrap tương thích.

| Variant | Hành vi |
|---|---|
| `sa_shared_tools_rag` | Một controller thực sự, được dùng cùng công cụ và RAG |
| `ma_fixed_rag` | Các chuyên gia chạy theo template cố định |
| `ma_adaptive_rag` | Các chuyên gia và continuation được chọn theo evidence |
| `ma_adaptive_no_rag` | Cùng bản adaptive nhưng tắt RAG |

Ba variant đầu dùng cùng model snapshot, corpus, embedding, tool contracts và giới hạn tài nguyên. Không đổi tên baseline product-only thành single-agent.

**Bộ câu hỏi**

80 hội thoại: 20 development và 60 held-out test, gồm:

- 12 tìm/lọc theo nhiều ràng buộc.
- 12 hỏi tri thức và kết hợp nguồn.
- 12 so sánh/recommendation cần nhiều chuyên gia.
- 8 hội thoại nhiều lượt và memory.
- 8 thiếu nguồn, sai ấn bản, mâu thuẫn hoặc injection.
- 8 shopping/merchant và ranh giới thao tác.

Tách dev/test theo nhóm tác phẩm; paraphrase đi cùng nhóm gốc. Mỗi case có identity fixture, facts bắt buộc/cấm, evidence span, khả năng trả lời và thao tác được phép.

Gold được soạn trước khi chạy SUT, không lấy đáp án hệ thống để sinh gold. Phần đánh giá ngữ nghĩa phải ghi rõ là tự động hay có người chấm; không gọi kết quả model judge là human evaluation. Xuất gói đáp án ẩn tên variant để nhóm có thể kiểm tra bổ sung.

**Pilot và ngân sách**

- Chạy tám case development × bốn variant, một lần; thêm warmup có ghi chi phí.
- Phân bổ dự kiến: 5 USD cho corpus, 10 USD pilot, 70 USD benchmark, 15 USD dự phòng.
- Benchmark mặc định ba lần lặp; nếu dự toán vượt phần ngân sách benchmark thì chốt hai lần lặp cho tất cả variant trước khi chạy.
- Không tự giảm số lượt của riêng một variant.
- Checkpoint sau mỗi observation, hỗ trợ resume mà không gọi lại các lượt đã hoàn thành.
- Hết ngân sách hoặc lỗi môi trường tạo báo cáo partial có danh sách thiếu; không công bố đó là benchmark hoàn chỉnh.

**Chỉ số**

Task completion, answerability/abstention, claim support, citation precision/coverage, document recall, quyền truy cập, valid-plan rate, useful continuation, duplicate dispatch, latency, token và chi phí.

Giữ `exact_plan` cho regression cũ; không phạt kế hoạch thích nghi chỉ vì khác template cố định.

Đo trên cùng trạng thái khởi đầu; reset sandbox/memory giữa các observation, giữ liên tục trong từng hội thoại. Dùng paired comparisons và confidence interval theo nhóm case gốc. Kết quả không thắng baseline vẫn phải được báo cáo trung thực.

## 5. Kiểm thử, bàn giao và điều kiện hoàn thành

### Kiểm thử bắt buộc

| Nhóm | Tình huống phải chứng minh |
|---|---|
| Tương thích API | OpenAPI chỉ công bố `/api/v2`; `/api/v1` routes trả 404 |
| Điều phối | Chọn đúng capability; không bỏ yêu cầu rõ; chỉ một continuation; không lặp bước đã xong |
| RAG | Mapping đúng tác phẩm/ấn bản; ACL; no-answer; corpus version; embedding mismatch |
| Grounding | Citation giả, sai entity/số liệu, phủ định, nguồn mâu thuẫn, verifier lỗi |
| Memory | Cập nhật/xóa; ưu tiên yêu cầu hiện tại; không lẫn người dùng hoặc chế độ |
| Sandbox | Stale preview, hết hàng, xác nhận đồng thời, retry trước/sau commit, hủy và confirmation đua nhau |
| Recovery | Mất SSE, worker dừng, restart, duplicate turn và đọc lại kết quả |
| Ngân sách | Retry/warmup/embedding đều được tính; reserve đồng thời; usage không rõ; stop/resume benchmark |
| Giao diện | Gửi/hủy/hỏi tiếp, mở nguồn, xem revision, duyệt/từ chối, load lại history, bàn phím/mobile |

Chạy PostgreSQL integration thật cho transaction/locking; SQLite và fakeredis không thay thế các kiểm tra đó.

Chạy lint, format-check, mypy, pytest và frontend lint/test/build. Với thay đổi cả Python và frontend, chạy hai nhóm kiểm tra rõ ràng; không dựa riêng vào `-Scope auto`, vì verifier hiện ưu tiên Python khi phát hiện cả hai.

### Luồng demo cuối cùng

1. Người mua hỏi sách theo nhiều tiêu chí.
2. Supervisor chọn chuyên gia và hiển thị tiến trình thật.
3. Một trường hợp thiếu evidence kích hoạt continuation.
4. Câu trả lời kết hợp catalog/review/tài liệu, mở được nguồn.
5. Follow-up hiểu sách và ràng buộc đang nói tới.
6. Ghi nhớ rồi xóa một sở thích.
7. Tạo giỏ và đơn sandbox qua xác nhận.
8. Merchant đề xuất chỉnh giá/tồn kho; shopper không được duyệt.
9. Thử stale proposal hoặc retry; không phát sinh thao tác trùng.
10. Restart dịch vụ, mở lại hội thoại và kết quả đã lưu.

### Bàn giao

- Nhánh `thanh-v2` với các commit tích hợp rõ ràng.
- Chatbot chạy local qua môi trường phát triển và cấu hình Compose local.
- Corpus/source/index manifests cùng báo cáo coverage.
- Bộ kiểm thử và báo cáo xác minh, gồm `VERIFY_STATUS` khi dùng verifier.
- Benchmark v3, protocol, usage ledger, biểu đồ/bảng kết quả và phân tích lỗi.
- Tài liệu cài đặt, kiến trúc, dữ liệu, đóng góp mới, giới hạn và kịch bản bảo vệ.
- Danh sách công việc deploy được tách riêng cho giai đoạn sau.

**Chỉ coi đợt triển khai hoàn thành khi các kiểm tra bắt buộc đạt, luồng demo xuyên suốt hoạt động và trạng thái benchmark được báo cáo chính xác.**

Hiện mới kiểm tra và lập kế hoạch: chưa tạo nhánh, sửa file hay chạy test. Trước triển khai cần xử lý môi trường Python và quyền truy cập Docker daemon; Docker CLI có trên máy nhưng phiên hiện tại chưa truy cập được daemon. Các việc này thuộc bước đầu của kế hoạch, không phải lý do bỏ qua kiểm thử tích hợp.
