# Protocol xác nhận luận điểm kiến trúc Multi-Agent

**Trạng thái: DRAFT — chưa khóa, chưa phải protocol đăng ký trước.**

Protocol này định nghĩa cách phân biệt bằng chứng phát triển với bằng chứng xác nhận. Không chạy SUT trên tập xác nhận mới cho tới khi dữ liệu, rubric, model, prompt, cấu hình, mã đánh giá, ngân sách và mã nguồn được đóng băng bằng hash.

## 1. Câu hỏi và đóng góp kiến trúc

**Câu hỏi chính:** Trên cùng yêu cầu, dữ liệu catalog, nguồn tri thức, model và giới hạn tài nguyên, chính sách Adaptive Multi-Agent của `app/v2` có tăng tỷ lệ câu trả lời vừa đúng vừa được nguồn hỗ trợ so với Single-Agent hay không?

Đóng góp được đánh giá là **gói điều phối Adaptive Multi-Agent có giới hạn**: specialist xử lý theo capability, planner điều chỉnh kế hoạch dựa trên evidence thu được, và cho phép tối đa một continuation trước khi tổng hợp câu trả lời có grounding. Không tuyên bố riêng từng specialist, router, planner hay synthesis là đóng góp đã được chứng minh.

Các đối chiếu có ý nghĩa:

| Đối chiếu | Diễn giải được phép |
| --- | --- |
| Adaptive MA + RAG so với SA + RAG | Hiệu quả của chính sách Adaptive MA tổng thể so với controller đơn. Đây là đối chiếu chính. |
| Fixed MA + RAG so với SA + RAG | Tín hiệu về specialist composition khi chưa bật adaptive planning/continuation. |
| Adaptive MA + RAG so với Fixed MA + RAG | Ablation của gói adaptive planning và continuation; không tách riêng được hai thành phần này. |
| Adaptive MA + RAG so với Adaptive MA không RAG | Đóng góp của retrieval trong chính sách Adaptive MA. |

Các arm phải gọi runtime `app/v2` đã ghép trong `app/evaluation/v3_variant_runtime.py`. Bộ đánh giá v3 là harness; nó không thay thế SUT. Trước khi khóa phải lưu đường dẫn import/call path và hash source thực tế. `product_ids` của gold chỉ là oracle để chấm; ca shopper phải bắt đầu với `resolved_product_ids=()` để runtime tự phân giải từ câu hỏi và lịch sử hội thoại. Chỉ fixture merchant được phép cấp target đã chọn rõ ràng như state phía máy chủ. P8 r6 đã truyền gold product IDs vào context shopper; executor được sửa và commit `4238394ee932dbf79a53aaa1a565c5203f857263`, sau đó P7/P8 r7 chạy lại. r7 vẫn chỉ là development regression, không phải tập xác nhận. Trace r7 cho thấy capability sequence Adaptive/Fixed trùng trên các task đã dùng, nhưng đó chỉ là tín hiệu development. Commit `fc1bd3f` từng tắt planner model cả Adaptive; code hiện tại đã được sửa ở `6cc2d8c` để khôi phục đúng bảng policy: SA và Adaptive dùng planner model, Fixed dùng kế hoạch xác định, còn continuation chỉ bật ở Adaptive. P7/P8 chưa chạy lại trên commit `6cc2d8c`; mọi kết quả trước đó không đại diện cho binding này.

## 2. Phân loại dữ liệu

- Toàn bộ `evaluation/v3/review-successor-v1` là **development** vì tập held-out đã được dùng trong remediation. Các lần chạy P7/P8 trước đây và các pilot trace `post1`–`post16` cũng là development.
- `evaluation/v3/review-challenge-v1` là development: packet được thiết kế sau khi đã biết các lỗi cần sửa, operator lock và human audit còn pending.
- Snapshot eval hiện dùng có 200 sản phẩm. Sau khi loại sản phẩm đã xuất hiện trong gold cũ và challenge cũ, còn 173 bản ghi nhưng chỉ 172 tên sách chuẩn hóa duy nhất. Đây chỉ là trần ứng viên từ snapshot nhỏ, trước audit trùng tác phẩm, prompt và oracle.
- Đã tái tạo profile `full` từ archive Kaggle Tiki Books v4 đã pin trong manifest: archive SHA-256 `371a618ed66425f8f2738ab01514ab2f20ed1bed645258b509fcce443bd32bbb`, normalized snapshot SHA-256 `c76a52fa6b3c3c3a934537699998c460a95226320861f4935f04560240b06e09`, 1.777 sản phẩm và 76.755 review sau pipeline làm sạch/redact. Script `evaluation/v3/thesis-validation-v1/audit_candidate_pool.py` loại toàn bộ 200 external ID trong snapshot eval, chuẩn hóa tên/tác giả bằng NFKD + casefold + bỏ dấu + bỏ cụm ngoặc chỉ rõ tái bản/định dạng, rồi đếm 1.577 product record và 1.553 nhóm tên sách/tác giả. Bộ lọc nhãn/tác giả an toàn và giá/rating của generator còn 1.439 nhóm đủ điều kiện; 1.268 nhóm trong số đó có số trang. Đây là pool ứng viên chưa audit, không phải nhóm tác vụ độc lập đã được xác nhận. Archive có dữ liệu review; chỉ dùng normalized/redacted snapshot, không đưa comment thô vào prompt hoặc gửi ra ngoài.
- Pool trên cùng nguồn Kaggle/Tiki không phải nguồn tri thức độc lập mới. Nó có thể cung cấp task set mới theo product ID/tác phẩm cho xác nhận catalog nếu được khóa và audit; không đủ để xác nhận khái quát sang nguồn/miền khác. Cả 20 source ID trong corpus ghi chú sách hiện tại đã xuất hiện trong split cũ, nên document recall trên knowledge corpus đó vẫn chỉ là development. Muốn kết luận document recall độc lập phải bổ sung nguồn mới có authority, license và hash riêng.

## 3. Thiết kế xác nhận cần khóa

- **Bốn arm:** SA + RAG, Fixed MA + RAG, Adaptive MA + RAG, Adaptive MA không RAG.
- Mỗi arm nhận cùng prompt, snapshot, corpus, identity, quyền, model, giới hạn token/call/deadline và lịch thực thi. Khác biệt chỉ theo policy đã khai báo; No-RAG chỉ tắt knowledge retrieval.
- **Đơn vị phân tích:** nhóm tác vụ độc lập theo tác phẩm/sản phẩm sau khi gộp edition hoặc title trùng. Nếu một nhóm có nhiều prompt hay repetition, gộp thành một kết quả nhóm; không đếm mỗi lượt chạy là một mẫu độc lập.
- Mỗi task-group có một prompt đóng băng và một measured response trên mỗi arm; lịch arm được ngẫu nhiên hóa/đan xen. Không nhân cỡ mẫu theo bốn arm. Repeat P8 cũ vẫn là regression/lifecycle gate riêng; nếu muốn thêm repeats cho validation mới, phải chốt phép gộp trong protocol và tái dự toán ngân sách trước khi mở tập.
- **Chỉ số chính:** tỷ lệ tác vụ mà câu trả lời bao phủ đủ mọi fact bắt buộc, mọi fact đó đúng với snapshot/oracle, và từng claim đều được chính citation của nó hỗ trợ. Nhãn là nhị phân theo nhóm tác vụ.
- **Đối chiếu chính:** Adaptive MA + RAG trừ SA + RAG. Ước lượng chênh lệch điểm phần trăm ghép cặp; báo cáo CI 95% bằng bootstrap theo nhóm tác vụ và kiểm định ghép cặp đã đăng ký trước.
- **Mức cải thiện tối thiểu quan tâm (MCID):** +5 điểm phần trăm. Đây là một câu trả lời đúng-có-nguồn tăng thêm trên mỗi 20 tác vụ; dưới mức đó, lợi ích khó biện minh cho độ phức tạp và chi phí điều phối. Estimand chính là chênh lệch ghép cặp `Adaptive MA − SA` theo nhóm tác vụ độc lập. Cổng xác nhận mức cải thiện có ý nghĩa thực tế yêu cầu cận dưới CI 95% hai phía lớn hơn `+5pp`; chỉ có ước lượng điểm `>= +5pp` và CI không chứa 0 chưa đủ để kết luận mức tăng vượt MCID.

### Cỡ mẫu và lực kiểm định

Ước lượng lập kế hoạch cho kiểm định ghép cặp, alpha hai phía 0,05, power 80%. Để có lực kiểm định cho cổng `cận dưới CI 95% > +5pp`, giả định hiệu ứng thật là `+10pp` và tỷ lệ bất đồng giữa hai arm `D = p10 + p01 = 0,20`. Khoảng cách cần phát hiện so với biên MCID là `5pp`:

| Tỷ lệ bất đồng giả định `D` | Hiệu ứng thật dùng để lập kế hoạch | Số nhóm độc lập xấp xỉ |
| ---: | --- | ---: |
| 0,10 | +10pp (5pp trên MCID) | 314 |
| 0,20 | +10pp (5pp trên MCID) | 628 |
| 0,30 | +10pp (5pp trên MCID) | 942 |

Các con số tái tạo bằng [`paired_power.py`](../evaluation/v3/thesis-validation-v1/paired_power.py). Công thức dùng xấp xỉ chuẩn bảo thủ `Var(paired difference) <= D`; đây là tính toán lập kế hoạch, không phải power đã được xác nhận trên dữ liệu mới. Với pool hiện tại có 1.553 nhóm title-author thô, bộ lọc nhãn/thuộc tính an toàn của generator còn 1.439 nhóm đủ điều kiện. Candidate chọn 1.256 nhóm cho 628 task, còn 183 nhóm đủ điều kiện chưa dùng; các số này được ghi riêng trong `candidate-pool-audit.json` và manifest candidate. Việc đạt cỡ mẫu chỉ khả thi nếu audit xác nhận không có edition/tác phẩm trùng, mỗi task có oracle rõ và đúng, và không có rò rỉ prompt/fact từ development. Nếu audit làm giảm số nhóm độc lập dưới cỡ mẫu cần thiết, phải mở rộng pool hoặc hạ tham vọng kết luận trước khi khóa; không được giảm cỡ mẫu dựa trên kết quả validation.

`D=0,20` vẫn là giả định lập kế hoạch, chưa phải ước lượng đã xác nhận. Cần ước lượng bất đồng bằng nhãn development độc lập trước khi khóa; nếu `D` cao hơn, tăng cỡ mẫu theo công thức/đăng ký lại. Không được giảm cỡ mẫu dựa trên kết quả validation. Bảng dùng xấp xỉ chuẩn cho chênh lệch nhị phân ghép cặp; trước khi khóa cần xác nhận con số bằng tính toán power đã lưu cùng script/version. Mẫu mới phải chứa ít nhất số nhóm độc lập theo giả định đã khóa. Task trên metadata catalog chỉ đo khả năng xử lý/so sánh catalog và không thay thế task có relevant knowledge document; báo document/evidence recall riêng theo task/source inventory đã định trước.

Task completion dùng biên không thua kém đề xuất `−5pp` so với SA. Báo cáo CI một phía theo protocol và nói rõ nếu mẫu không đủ lực để kết luận không thua kém. Không suy ra non-inferiority từ p-value không có ý nghĩa.

## 4. Chấm chất lượng và kiểm soát sai số

- Hai người chấm độc lập, không biết arm/model/run ID, chấm câu trả lời theo [rubric dự thảo](../evaluation/v3/thesis-validation-v1/human-rubric.md). Thứ tự câu và nhãn hiển thị được ngẫu nhiên hóa; khóa ánh xạ arm chỉ mở sau khi chốt nhãn.
- Rubric yêu cầu đánh giá từng claim, tính đúng, nguồn có hỗ trợ claim cụ thể hay không, độ đầy đủ, citation precision/coverage, hành vi trả lời/abstain và task completion. Hướng dẫn phải có ví dụ tiếng Việt, paraphrase hợp lệ, sai số số học, nhầm phủ định và citation không hỗ trợ.
- Báo cáo agreement trước hòa giải (Cohen’s kappa hoặc Krippendorff’s alpha phù hợp loại nhãn), số bất đồng và quy tắc adjudication bởi người thứ ba. Không sửa nhãn im lặng.
- Có thể dùng LLM judge để chấm toàn bộ câu trả lời, nhưng chỉ đưa nhãn đó vào kết quả xác nhận sau khi hiệu chuẩn với người chấm mù. Judge model, phiên bản, prompt, schema, sampling và rubric phải được khóa trên development; chọn judge model khác model sinh câu trả lời và không tinh chỉnh judge trên tập xác nhận. Cấu hình P7/P8 r7 bind `gpt-5.4-mini-2026-03-17`, cùng model với SUT; chưa có nhãn judge và cấu hình đó không đủ điều kiện cho judge độc lập của xác nhận mới. Đổi model judge làm thay đổi evaluator binding, nên phải khóa protocol mới trước khi chạy P7/P8 tương ứng.
- Ngày 2026-10-13 đã tạo 32 nhãn tham chiếu bằng `gpt-6-luna` cho 32 observation đo của pilot P7 (8 ca development × 4 nhánh), nhằm hiệu chuẩn judge P8 sau này. Packet, output, request audit và hash được lưu tại [`dev-judge-calibration-20261013`](../evaluation/v3/thesis-validation-v1/dev-judge-calibration-20261013/README.md). Đây là reference giữa hai LLM, không phải human gold hay kết quả chấm P8. Chưa chạy 752 lượt judge P8 hoặc so sánh nhãn judge `gpt-5.4-mini-2026-03-17` với 32 reference này.
- Judge chấm từng câu trả lời riêng lẻ, theo task ID giả danh đã xáo trộn; không thấy arm/model/run ID và không so sánh hai câu trả lời cạnh nhau. Nó nhận prompt, oracle đã khóa, câu trả lời, citation cùng các nguồn snapshot liên quan; phải trả nhãn theo rubric và chỉ ra claim/source làm căn cứ. Người chấm nhận cùng packet nhưng không thấy nhãn judge.
- Trước khi chạy judge trên xác nhận, lấy ngẫu nhiên có phân tầng một tập task-group để hai người chấm độc lập; lấy mọi arm của các group đã chọn để giữ phép so sánh ghép cặp. Tách phần dùng để hiệu chỉnh prompt khỏi phần human holdout chỉ dùng kiểm định judge. Không chọn mẫu dựa trên việc judge đã bất đồng hay kết quả arm nào thuận lợi.
- Đăng ký trước ngưỡng người-người và judge-người cho nhãn primary, cùng sai số tối đa chấp nhận được đối với chênh lệch Adaptive MA–SA; cỡ mẫu human holdout phải đủ hẹp để kiểm tra các ngưỡng đó. Báo cáo agreement, confusion matrix, tỷ lệ lỗi theo arm/answerability và chênh lệch primary effect giữa nhãn người với nhãn judge, kèm CI. Nếu chưa đạt ngưỡng hoặc human holdout thiếu lực, nhãn LLM chỉ là thăm dò; không dùng riêng nó để tuyên bố chất lượng đã được xác nhận.
- Nếu không thể bố trí người chấm, vẫn có thể báo kết quả LLM-only với prompt/model đã khóa, nhưng phải gọi đó là đánh giá tự động thăm dò; điều kiện xác nhận chất lượng ngữ nghĩa của protocol này chưa đạt.
- Authorization: lập danh mục đầy đủ các ca phân quyền trọng yếu từ policy/registry `app/v2`; chạy tất cả ca đã đăng ký và yêu cầu không có vi phạm. Tập này là safety gate riêng, không gộp thành hàng nghìn lượt độc lập.

## 5. Chỉ số và thống kê

Chỉ số chính duy nhất là correctness-and-source-support ở trên. Báo cáo thêm task completion, answerability/abstention, claim correctness, citation precision, citation coverage, document/evidence recall, authorization, valid plan, useful continuation, duplicate dispatch, latency end-to-end p50/p95, token và chi phí.

Document recall chỉ báo khi mỗi task có relevant document/source ID được xác định trước và runtime trace ghi nhận retrieval; citation trong câu trả lời không tự chứng minh document đã được truy hồi. Các kiểm định phụ trong cùng họ dùng hiệu chỉnh Holm. Báo cáo cả kết quả bất lợi, thiếu citation, fallback, timeout, lỗi và mọi cell thiếu; không chọn metric thuận lợi sau khi xem dữ liệu.

## 6. Chi phí và độ trễ

Ngân sách per-task, giới hạn call/token, timeout và concurrency phải giống nhau giữa các arm. Dùng mức đã đóng băng trong experiment hiện tại: tối đa `$0.25/task`, deadline `60 giây/task`, cùng giới hạn generation/retry/concurrency. Mọi timeout, fallback và lượt vượt cap vẫn nằm trong mẫu và bị tính là lỗi vận hành; không bỏ chúng khỏi mẫu latency/chất lượng. Đo latency từ đầu tới cuối trên cùng máy/DB.

Báo p50/p95 latency, token/task và USD/task theo từng arm cùng chênh lệch ghép cặp. **Cổng kết luận Adaptive MA đạt mức cải thiện tối thiểu đã định** phải đồng thời đạt: (a) cận dưới CI 95% hai phía của primary metric `Adaptive MA − SA` lớn hơn `+5pp`; (b) cận dưới CI một phía của chênh lệch task completion lớn hơn `−5pp`; (c) không vi phạm authorization trọng yếu; (d) p95 dưới deadline 60 giây và chi phí mỗi task không vượt cap `$0.25`. Mức +5pp là MCID đã giải thích ở mục 3; cap latency/chi phí lấy từ protocol runtime hiện hành, không chọn lại sau khi xem kết quả. Nếu ước lượng primary metric dương nhưng CI chưa vượt `+5pp`, báo kết quả là chưa đủ bằng chứng cho mức cải thiện tối thiểu, kể cả khi một metric phụ thuận lợi.

### Ước lượng ngân sách cho candidate 628 task

Candidate hiện tại tương ứng `628 × 4 = 2.512` measured cells. Checkpoint development `run_ma_trace_20261012_post16` có 32/32 measured cells hoàn tất (8 mỗi arm), checkpoint SHA-256 `a4150784ff9335a2c70b8d9f39d2f91b5c9013708cc956e4c22ec7660ece5a81`, protocol SHA-256 `580e07f88fd9784ff4b42fe9270c2089f033bbc36b1e2effdcf4157c124dc5d1`. Từ 8 cell/arm đó, chi phí trung bình là: SA `$0.00762`, Fixed MA `$0.00645`, Adaptive MA `$0.01089`, Adaptive MA không RAG `$0.00168` mỗi task. Nhân trung bình theo 628 task cho ước lượng SUT `$16.73`; nhân mức cao nhất từng thấy ở mỗi arm cho kịch bản stress `$53.67`. Thời gian tuần tự theo trung bình là khoảng `7,55 giờ`, chưa tính retry, judge và vận hành.

Đây chỉ là ước lượng lập kế hoạch từ 8 case/arm trên source chưa có Git SHA; không phải dự toán đã khóa hay trần chi phí. Theo cap judge `$0.086/job` của gói 752 lượt trước, 2.512 judgment có ceiling `$216.032`, chưa gồm hiệu chuẩn reference mới. Quyền duyệt gói 752 lượt trước không bao gồm gói lớn hơn này. Tính lại ngân sách sau pilot trên commit hiện hành, bổ sung nguồn/RAG nếu có, rồi xin duyệt chính xác package mới trước khi gửi dữ liệu xác nhận. Candidate catalog hiện tại không có cohort tài liệu mới nên chưa thể dùng projection này để tuyên bố về document recall hoặc hiệu quả RAG.

## 7. Tái lập và lifecycle

Gói khóa cần chứa SHA-256 của task/gold/split, snapshot catalog, từng source, corpus/index, model và embedding ID, prompt/config, rubric/judge, evaluator source, pricing/ledger contract, lịch chạy và ngân sách. Gắn SUT với Git commit cụ thể; không chấp nhận `NOT VERIFIED` hoặc commit rỗng. Lưu raw output, citation, runtime trace, token/cost ledger, lỗi, judgment người đã ẩn danh và script tái tạo.

Sau source change, lifecycle vẫn là P7 → P8 đúng 720/720 → operate trên cùng binding/commit. Do held-out cũ đã dùng để sửa hệ thống, lần chạy này chỉ là regression/lifecycle evidence và phải gắn nhãn development; không được gọi là independent validation. 720/720 chỉ chứng minh đủ coverage.

## 8. Điều kiện để chuyển DRAFT thành LOCKED

1. Mở rộng ít nhất lên cỡ mẫu đã đăng ký, gộp edition/title trùng và hoàn tất audit oracle độc lập trước khi gửi prompt tới SUT.
2. Bổ sung tập source mới nếu muốn tuyên bố về document recall/RAG; ghi authority, license, raw hash và mappings.
3. Hoàn tất rubric, người chấm, quy tắc hòa giải, calibration judge, biên task-completion, ngưỡng latency/cost và phép hiệu chỉnh nhiều chỉ số.
4. Chốt model, embedding, prompt, config, snapshot DB/corpus, exact SUT commit, protocol hash, API budget và run schedule; tạo `lock.json` trước khi gọi model.
5. Tạo blind scoring packet cùng answer-key riêng; không mở answer-key cho người chấm trước khi họ nộp nhãn độc lập.

## 9. Trạng thái sẵn sàng triển khai

- Snapshot `tiki-books-v4-full` đã qua kiểm tra artifact cục bộ: 1.777 sản phẩm, 76.755 review, snapshot SHA-256 `c76a52fa6b3c3c3a934537699998c460a95226320861f4935f04560240b06e09`. Script audit loại 200 product ID của snapshot eval cũ và hiện tìm 1.553 nhóm title-author ứng viên; nhóm này chưa được kiểm tra thủ công về edition/tác phẩm trùng hoặc oracle.
- Runner v3 hiện không nhận tập xác nhận 628 nhóm: `EvaluationGoldV3` và split v3 khóa đúng 80 entry, experiment P7 khóa 8 pilot case. P8 720 ô là lifecycle gate trên 60 held-out case × 3 lần lặp × 4 arm. Không được diễn giải các giới hạn này thành cỡ mẫu xác nhận độc lập.
- Runner v2 nhận tối đa 500 case và gọi `app.orchestrator` qua `app/evaluation/variant_runtime.py`; đường này không phải runtime `app/v2` đang được xác nhận. Dùng runner đó cho tập mới sẽ không đáp ứng ràng buộc kiến trúc của đề tài.
- Cần tạo đường chạy xác nhận riêng có schedule động, nhưng tái sử dụng executor v3 gọi runtime `app/v2`, quyền, budget, reset, receipts và trace. Bản protocol mới phải khóa case roster/hash, nguồn, rubric/judge, model, runtime, schedule và phân tích trước khi thực thi. Run P7/P8 hiện tại giữ vai trò regression/lifecycle gate của protocol cũ.
- Chưa có tập xác nhận mới được tạo hoặc gửi tới SUT; chưa có judge hoặc human scoring cho P8 r7. Payload gửi judge đã được duyệt ngày 2026-10-13, nhưng lượt xử lý 752 judgment chưa chạy vì service evaluation hiện thiếu role DB dành cho runner; auto-review từ chối thao tác tạo role và cấp quyền, nên không dùng đường vòng. Không gọi benchmark xác nhận trước khi packet, runner, oracle, judge và ngân sách được khóa.
