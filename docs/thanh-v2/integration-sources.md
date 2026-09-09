# Nguồn tích hợp chọn lọc cho `thanh-v2`

Tài liệu này cố định provenance cho các phần dự kiến tham khảo hoặc chuyển thể từ
`lac-v2`. Đây là kế hoạch tích hợp, không phải xác nhận rằng mã nguồn đã được port.
Không merge toàn bộ nhánh nguồn; mỗi thay đổi sau này phải được đưa vào từng phần, có
diff và kiểm thử riêng.

## Snapshot đã kiểm chứng

| Vai trò | Repository / nhánh | Commit đầy đủ | Tác giả commit | Ghi chú |
|---|---|---|---|---|
| Đích | `Multi-Agent`, `thanh-v2` | `b7df1cc420127e31449a777204411bf6844f5b65` | Hoang Xuan Thanh | Base của `thanh-v2` khi bắt đầu tích hợp; subject `feat: complete Evidence Atlas optimization`. |
| Nguồn cố định | `Multi-Agent-lac-v2`, `lac-v2` | `94af718da1858b74b3cb4fba05ddd908ac28d9b4` | Tran Gia Lac | Subject `Migrate commerce agents to OpenAI and add agentic RAG platform`; commit này thêm toàn bộ `commerce-common/commerce_common/rag/`, `commerce-common/tests/test_rag.py` và `docs/commerce-rag.md`. |
| Nguồn gốc proposal/approval có trước snapshot | parent của commit nguồn | `fd4d59224ab96b43c6dc6888207c67b3bd5a24cf` | Ali Shazal | Subject `building commerce agents using claude`; `git blame` xác nhận các model/gate cốt lõi của staged change và host approval bắt nguồn ở đây. Chỉ tham khảo phần provider-neutral. |

Cả hai checkout khai báo cùng remote
`https://github.com/Thanh281105/multi-agent-system.git`. Checkout nguồn đúng commit
`94af718da1858b74b3cb4fba05ddd908ac28d9b4`; thay đổi duy nhất ngoài commit là
`.gitignore`. File bẩn đó không được đọc để lấy thiết kế, không được sửa và không nằm
trong bất kỳ mapping nào dưới đây.

## Giấy phép và attribution bắt buộc

Snapshot nguồn dùng Apache License 2.0 (`LICENSE:2-4`). Điều 4 yêu cầu cung cấp một bản
sao giấy phép cho bên nhận (`LICENSE:90-96`), đánh dấu rõ file đã sửa
(`LICENSE:98-99`) và giữ các thông báo copyright, patent, trademark và attribution còn
liên quan (`LICENSE:101-105`). Điều kiện `NOTICE` chỉ phát sinh khi Work có file
`NOTICE` (`LICENSE:107-122`); `git ls-tree` xác nhận snapshot nguồn không có
`NOTICE`, `NOTICE.txt` hoặc `NOTICE.md`.

Khi bắt đầu Package3, bản sao chính xác giấy phép tại commit nguồn đã được thêm ở
[LICENSES/Apache-2.0.txt](../../LICENSES/Apache-2.0.txt), SHA256
`cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`.
Thông báo thay đổi trên từng file và bảng provenance vẫn phải đi kèm code thực tế;
bản sao giấy phép riêng chưa có nghĩa là đã hoàn thành port.

Các file mã liên quan đều mang:

```text
Copyright 2026 Anthropic PBC
SPDX-License-Identifier: Apache-2.0
```

Khi bắt đầu port mã, mỗi file chứa phần sao chép hoặc chuyển thể đáng kể phải giữ hai
dòng này và thêm thông báo nổi bật rằng file đã được sửa cho `thanh-v2`, kèm commit
nguồn đầy đủ. Trước khi phân phối phần đã port, bổ sung bản sao Apache-2.0 ở
`LICENSES/Apache-2.0.txt` (hoặc một vị trí giấy phép tương đương được dự án chốt) và
attribution dễ đọc nêu hai commit/tác giả ở bảng trên. Tại baseline target chưa có `LICENSE`,
`NOTICE` hay `COPYING`; Package3 bổ sung bản sao ở vị trí nêu trên. Không tự tạo
copyright mới cho người tích hợp nếu chủ sở hữu chưa được xác định.

## Bản đồ source → target

Tên target dưới đây là đích dự kiến cho lần port tăng dần. Nếu bước triển khai phải đổi
tên file để theo migration hoặc module hiện có, attribution vẫn bám theo source path và
symbol, không bám theo tên file target.

| Phần | Source tại `94af718…` và bằng chứng | Target dự kiến | Cách sử dụng |
|---|---|---|---|
| Contract tài liệu, ACL và evidence | `commerce-common/commerce_common/rag/models.py`: `RagAcl`, `RagAccessContext.permits`, `CommerceDocument`, `RagDocumentArtifact`, `RagChunk` (dòng 16-88); `QueryPlan`, `Citation`, `RetrievalHit`, `RetrievalBundle`, `GroundingReport` (dòng 91-168) | `app/knowledge/models.py`, migration/model PostgreSQL tương ứng, schema response v2 | Chuyển thể model và test invariants. Giữ tenant/principal/role do server cấp và metadata nguồn/version/hash. Đổi `citation_id="C#"` thành evidence ID ổn định theo source/version/chunk; `C#` chỉ là nhãn hiển thị trong một response. Kết nối với `app/contracts/a2a.py::DataProvenance`, không thay schema v1. |
| Chunking và ingestion idempotent | `commerce-common/commerce_common/rag/ingestion.py`: `TableAwareChunker` (230-292), `CommerceIngestor._hash/ingest` (303-409) | `app/knowledge/ingestion.py`; repository và migration knowledge | Dùng cách hash nội dung, bỏ qua bản không đổi, enrichment sau khi chia chunk và batch embedding. Sửa thuật toán bảng: source cố giữ nguyên bảng dài ở dòng 275-279, còn contract `thanh-v2` yêu cầu tách theo hàng và lặp header trong giới hạn token. Thêm corpus/index manifest và fingerprint embedding model, dimension, chunker, enrichment policy. |
| Embedding offline và boundary online | `commerce-common/commerce_common/rag/embeddings.py`: `tokens`, `normalize`, `HashEmbeddingProvider` (17-50), `OpenAIEmbeddingProvider` (53-81) | `app/knowledge/embedding.py`, `app/shared/embedding_runtime.py` | Có thể chuyển thể tokenizer/hash cho regression offline. Không port `OpenAIEmbeddingProvider`; dùng `EmbeddingRuntime`/`OpenAIEmbeddingRuntime` hiện có của target (model mặc định `text-embedding-3-small`, 1.536 chiều) để giữ validation, budget và telemetry chung. |
| PostgreSQL store và lọc quyền trước retrieval | `commerce-common/commerce_common/rag/store.py`: atomic `replace_document` (97-128), `accessible_documents` (183-190), `accessible_chunks` (211-228); `models.py::RagAccessContext.permits` (31-39) | `app/knowledge/postgres.py` cùng models/migrations knowledge | Chuyển thể contract và thứ tự lọc quyền; không copy SQLite/JSON-vector implementation. Tenant và ACL phải nằm trong truy vấn PostgreSQL, rồi kiểm tra defense-in-depth. Mỗi turn pin một corpus version. `app/knowledge/qdrant.py` tiếp tục tắt trong gói này. |
| Hybrid retrieval và rank provenance | `commerce-common/commerce_common/rag/retrieval.py`: `_bm25` (292-316), `RankedChunk` (319-327), `DeterministicPostProcessor` (340-397), `HybridRetriever.retrieve` (485-605) | `app/knowledge/retrieval.py` | Port BM25 + dense union + weighted RRF và provenance kênh/rank; port dedupe/adjacent expansion có giới hạn. Giữ mặc định 6, tối đa 8 và context tối đa 6.000 token theo contract đích. Thêm relevance threshold được hiệu chỉnh trên development set; source luôn tạo kết quả từ candidate union nên chưa có no-answer threshold tương ứng. |
| Query rewrite, source selection và post-processing | `commerce-common/commerce_common/rag/retrieval.py`: `AgenticQueryPlanner` (243-256), `QueryPlanner` (259-266), `OpenAIQueryPlanner` (269-283), `OpenAIPostProcessor` (400-482); ACL regression ở `commerce-common/tests/test_rag.py` dòng 248-278 và unknown-chunk regression dòng 308-324 | `app/knowledge/retrieval.py`, gọi qua `app/shared/model_runtime.py` | Dùng hợp đồng hai bước và allowlist ID sau lọc quyền. Viết lại lớp online trên `ModelRuntime.generate_structured`; không mang `ModelProvider.create` hoặc một provider loop riêng. Không cho model mở rộng quyền hay trả chunk ID ngoài candidate set. |
| Grounding | `commerce-common/commerce_common/rag/grounding.py`: `GroundingVerifier` (45-111), `OpenAIGroundingVerifier` (114-206); regressions ở `commerce-common/tests/test_rag.py` dòng 176-193 và 327-347 | `app/knowledge/grounding.py`, schema claim/evidence v2 | Chuyển thể kiểm tra citation hợp lệ, coverage và số liệu chính xác; semantic verifier dùng `ModelRuntime`. Lexical overlap chỉ là tín hiệu phụ. Bổ sung entity, negation, structured-fact checks và hành vi fail-closed/revise-once theo contract đích. |
| Facade và tool RAG | `commerce-common/commerce_common/rag/service.py`: `commerce_rag_tool_definitions` (13-54), `CommerceRag` (57-79); `commerce-common/commerce_common/execution.py`: injection/dispatch và `_search_knowledge`/`_verify_grounding` (135-183) | `app/knowledge/service.py`, registry riêng `app/mcp/v2_catalog.py` | Chuyển thể facade search/verify và JSON schema chặt. Đăng ký chỉ ở v2; không thêm field/tool vào registry cố định của v1. Quyền truy cập lấy từ request/session server, không nhận tenant/role từ tool arguments. |
| Hợp đồng tool và validation | `commerce-common/commerce_common/execution.py`: `parse_argument`, `clamp_limit`, `with_status`, `without_status`, `contracts_by_name` (47-96); `merchant-agent/core/merchant_agent/tools/registry.py` (đặc biệt schema write và `additionalProperties: false`, dòng 330-498) | `app/contracts/tools.py`, `app/mcp/v2_catalog.py` và adapter v2 | Dùng mẫu registry → schema → allowlist → executor, validation trước handler và giới hạn ở application code. Giữ `app/mcp/router.py::MCPToolSpec`/registry v1 nguyên hành vi; v2 cần contract có input/output schema, capability, read/write effect và confirmation policy. Không copy text/domain campaign không liên quan. |
| Proposal, preview và audit fields | `merchant-agent/core/merchant_agent/types.py`: `ChangeStatus`, `ChangeItem`, `StagedChange` (382-436); `executor.py::_staged` (252-267) | `app/sandbox/models.py`, `app/sandbox/service.py`, action-card schema v2 | Chuyển thể state machine, before/after diff, actor/timestamp audit và việc preview không tự duyệt. Mở rộng thành proposal ID/version, target/data version, expiry 10 phút và trạng thái expired/conflict/rejected. Dùng PostgreSQL làm nguồn bền vững. |
| Provenance gate, approval gate, apply/reject | `merchant-agent/core/merchant_agent/gates.py`: `check_listing_provenance` (113-124), `check_apply_change` (192-215); `executor.py::_apply_change/_discard_change` (350-374); `runtime-agent-sdk/.../merchant_tools.py::host_approve/host_clear` (90-106) | `app/sandbox/actions.py`, PostgreSQL repositories, API v2 action routes | Giữ nguyên tắc ID phải được đọc/stage trong context được phép, guardrail được kiểm tra lại lúc apply, và approval phải đến từ host route. Không port `approved_change_ids` dạng set trong session. Confirm/reject phải kiểm tra owner/role, expiry, proposal version, current offer/cart/inventory version và `Idempotency-Key` trong một transaction ngắn; không gọi model/network trong transaction. |
| Regression tests | `commerce-common/tests/test_rag.py` (100-193, 248-347); `commerce-common/tests/test_execution.py` (63-137); `merchant-agent/core/tests/test_executor.py`, `test_changes.py`, `test_gates.py`; `merchant-agent/runtime-agent-sdk/tests/test_agent.py` | `tests/test_knowledge_ingestion.py`, `tests/test_knowledge_retrieval.py`, `tests/test_grounding.py`, `tests/test_v2_tool_contracts.py`, `tests/integration/test_sandbox_actions.py` | Port ý định của test, không copy fixture commerce chung. Bắt buộc thêm case table-row splitting, stable evidence ID, corpus/fingerprint mismatch, no-answer threshold, ACL trước planner/ranking, fake citation, wrong entity/number/negation, stale preview, concurrent confirmation và idempotent retry trước/sau commit. Transaction/locking phải chạy với PostgreSQL thật. |

## Phần tái sử dụng và phần chủ ý loại trừ

### Các phần đã có diff và kiểm tra trong Package 3

| Target thực tế | Source tại `94af718da1858b74b3cb4fba05ddd908ac28d9b4` | Trạng thái và thay đổi |
| --- | --- | --- |
| `app/knowledge/v2_contracts.py` | `commerce-common/commerce_common/rag/models.py`: `RagAcl`, `RagAccessContext`, `CommerceDocument`, `RagDocumentArtifact`, `RagChunk`, `Citation` | `algorithm reimplemented`: dùng khái niệm typed document/chunk/ACL/evidence; validation, mapping tác phẩm/ấn bản, canonical hash, ID citation độc lập corpus và fingerprint được viết theo contract thanh-v2. Không sao chép provider loop. |
| `tests/test_knowledge_ingestion.py` (nhóm contract ban đầu) | Các khái niệm contract ở dòng trên; các tình huống lỗi riêng của thanh-v2 | Test mới kiểm tra normalization/identity tái lập, ID work/edition bị tráo, ACL không hợp lệ, vector NaN và chunk/span bị sửa. Lead chạy lại: 4 passed; Ruff lint/format và mypy đạt. |
| `app/knowledge/retrieval.py` | `commerce-common/commerce_common/rag/retrieval.py`, blob `4a1ac852bdd20ebe458e32a5e9d5d5be80db5f2b`: `_bm25`, `RankedChunk`, `HybridRetriever.retrieve`, `DeterministicPostProcessor.process`; ACL-order intent từ `store.py`, blob `646e633f358c74d36e545a51610523a778c345fb` | `adapted`: giữ BM25, dense/BM25 union, weighted RRF, dedupe và adjacent ordering. Bổ sung pin corpus/index, SQL-store protocol, current authorization, relevance/no-answer, diagnostics, cosine an toàn, context bound gồm metadata và cancellation qua ledger. Planner online được viết lại trên `ModelRuntime`; không port provider loop hoặc model postprocessor của source. |
| `app/knowledge/service.py` | `commerce-common/commerce_common/rag/service.py`, blob `e2267eac0e1e976ca3a616829d91c51bb9363ca0`: `CommerceRag.search` | `algorithm reimplemented`: dùng ý tưởng facade, viết mới chuyển đổi contract v2 và mở lại evidence theo source/version/chunk/span với quyền hiện tại. `[C#]` chỉ là label; timestamp là thời điểm nguồn được thu thập. Các tool definitions và grounding methods của source chưa được port bởi file này. |
| `tests/test_knowledge_retrieval.py`, `tests/test_knowledge_citations.py` | `commerce-common/tests/test_rag.py`, blob `60d6a84f00c4702892986e5173086eefb00a8c01`: ACL-before-planning, stable citations, unauthorized-source rejection, adjacent ordering | `test intent ported`, thêm tình huống riêng của thanh-v2: required/hybrid errors, budget/cancel propagation, source narrowing, Unicode context cap, finite-vector overflow và exact reopening. Lead chạy lại bản đã đóng băng: 28 passed; Ruff bốn file và mypy hai module đạt. Đây là regression qua fixture; PostgreSQL kết hợp và provider thật có gate riêng. |
| `app/knowledge/ingestion.py` | `commerce-common/commerce_common/rag/ingestion.py`, blob `d5d2f9583c5fa528647b8c7ce4b8bacebdf13701`: `TableAwareChunker`, `CommerceIngestor` | `adapted`: giữ cách chia block/sentence và enrichment trước batch embedding. Viết lại tách hàng bảng với header lặp và hard cap; thêm manifest bất biến, fingerprint, vector reuse, durable batch claim và kiểm tra publication. Giữ copyright/SPDX và thông báo sửa tại đầu file. |
| `app/knowledge/postgres.py` | `commerce-common/commerce_common/rag/store.py`, blob `646e633f358c74d36e545a51610523a778c345fb`: `replace_document`, `accessible_documents`, `accessible_chunks`; ACL contract trong `rag/models.py` | `algorithm reimplemented`: không copy SQLite store. PostgreSQL lưu draft, claim và vector theo transaction ngắn; lọc tenant, principal, mode và scopes trong SQL trước planner/ranking; mở lại exact source/version/chunk/span theo quyền hiện tại. |
| `tests/test_knowledge_ingestion.py`, `tests/test_knowledge_postgres.py` | `commerce-common/tests/test_rag.py`, blob `60d6a84f00c4702892986e5173086eefb00a8c01`: unchanged ingestion và ACL intent | `test intent ported`, bổ sung hard-cap bảng, version/fingerprint, claim chưa có kết quả, resume và concurrency riêng của thanh-v2. Lead chạy lại cùng calibration: 31 passed, gồm sáu ca PostgreSQL thật, không skip. Embedding trong các test này là fixture; publication với provider thật có gate riêng. |

`scripts/build_book_corpus.py` và bộ hiệu chỉnh development là code mới để nối các
contract thanh-v2 với ledger chung; không port provider loop hay benchmark runner
của source. Lead đã đối chiếu trực tiếp chunker/ingestor ở blob cố định và các
file target đã đóng băng trước khi ghi các dòng provenance trên.

Các dòng dự kiến ở bảng nguồn phía trên tiếp tục là kế hoạch cho tới khi có diff
và kiểm tra tương ứng; các target có dòng thực tế ở bảng này đã được tích hợp ở
phạm vi ghi rõ. Lead đối chiếu lại blob IDs, công thức BM25, adjacent ordering,
facade nguồn và header của bản retrieval/service đã đóng băng. Bản sao giấy phép Apache-2.0 đã lưu tại
`LICENSES/Apache-2.0.txt`; hash SHA-256 đã đối chiếu với blob giấy phép nguồn:
`cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`.

Sẽ tái sử dụng có chuyển thể:

- typed records cho document/chunk/retrieval trace, ACL filter-before-plan, content hash và
  incremental ingestion;
- BM25 + dense candidate union, RRF, dedupe và adjacent-context ordering;
- citation/grounding checks, cùng regression intent cho ACL và unknown IDs;
- JSON-schema tool contracts, application-side validation/limits và registry allowlist;
- state machine proposal → preview → approval → apply/reject, before/after diff, provenance
  gates, re-check guardrail và actor/timestamp audit.

Chủ ý không đưa sang:

- full branch merge, bảy package, example storefront/merchant UI, sample datasets hoặc
  file `.gitignore` bẩn của source;
- `claude_agent_sdk`, Messages API/Claude runtime, manifest Claude, prompt/provider loop
  thứ hai hoặc các adapter gọi `ModelProvider.create` trực tiếp;
- `SqliteRagStore`, vector dạng JSON trong SQLite và Qdrant runtime; target dùng
  PostgreSQL cho document/chunk/vector và Qdrant vẫn tắt;
- `OpenAIEmbeddingProvider` của source; target đã có embedding boundary có kiểm tra
  dimension/order/error;
- việc giữ nguyên bảng dài vượt cap, citation ID `C#` như định danh bền vững, retrieval
  luôn trả candidate không có relevance threshold, và lexical overlap như bằng chứng
  semantic cuối cùng;
- approval opt-out (`require_host_approval=False`), approval mark trong memory/session,
  sequence ID kiểu `chg-0001`, apply thiếu version/expiry/idempotency/locking, hoặc coi câu
  “đồng ý” trong hội thoại là authorization;
- campaign, promotion, listing-content workflow, thanh toán thật, SDK host toolset và
  LLM-as-judge/eval runner của source trong gói tích hợp này.

## Quy tắc cập nhật provenance khi port

Mỗi PR/commit tích hợp sau này phải cập nhật bảng trên với target file thật và một trong
ba trạng thái: `adapted`, `algorithm reimplemented`, hoặc `test intent ported`. Nếu không
có dòng nguồn được sao chép mà chỉ dùng ý tưởng/contract, ghi `algorithm reimplemented`.
Không đổi trạng thái thành “đã port” trước khi diff target và test tương ứng tồn tại.

Trước mỗi lần port cần kiểm tra lại đúng blob từ commit cố định, không đọc code từ HEAD
nguồn nếu HEAD đã thay đổi. Nếu source bổ sung `NOTICE` hoặc đổi license ở commit khác,
commit đó là nguồn mới và phải được ghi riêng; không được ngầm mở rộng snapshot này.
