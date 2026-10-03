# Evidence Atlas — Product brief

## Platform

web

## Delivery

Responsive web application được FastAPI phục vụ same-origin. Production UI dùng
React, Vite, TypeScript, Tailwind CSS và local shadcn/ui primitives, sau đó được
đóng gói thành static assets trong Python application.

## Product summary

Evidence Atlas là trợ lý quyết định sách tiếng Việt dựa trên **snapshot lịch sử
Tiki Books đã làm sạch**. Một câu hỏi được chuyển thành routing, authorized DAG,
domain-agent execution và grounded synthesis có provenance. Sản phẩm đồng thời
là workspace sử dụng được và artifact khóa luận có thể audit.

Hệ thống không phải công cụ duyệt catalog Tiki trực tiếp. Nó không biết tồn kho,
giá hiện tại, người bán hiện tại, xu hướng, nhu cầu hay thị phần.

## Primary users

- Người đọc cần tìm, so sánh hoặc xem tín hiệu review của sách có trong snapshot.
- Người chấm khóa luận cần kiểm tra agent nào chạy, plan nào được biên dịch và
  source nào hỗ trợ câu trả lời.
- Developer/operator cần correlation IDs, fallback, latency, token usage và
  nhãn dữ liệu lịch sử mà không nhìn thấy secret hay raw prompt.

## Core outcome

Trong một màn hình, người dùng có thể hỏi bằng tiếng Việt, nhận câu trả lời giới
hạn đúng theo snapshot và kiểm tra execution/provenance đứng sau kết luận.

## Positioning

Đây là evidence-first book-agent operations workspace, không phải chatbot mua
sắm tổng quát. Giá trị chính là kết hợp giao diện hội thoại gọn với dossier thực
thi trung thực: route, plan, specialist agents, model calls/fallbacks, provenance
và trace identity.

## Verified capabilities

- API v2 điều phối bounded expert services cho `product_agent`, `review_agent`,
  `trust_agent`, `market_agent`; dependencies và quyền do Python kiểm tra,
  execution tuần tự. Domain-agent classes cũ thuộc evaluation lịch sử.
- Product search/filter và ranking trên metadata sách; contract và giới hạn
  parser của từng runtime được ghi trong [kiến trúc](docs/architecture.md).
- Review/Trust phân tích sampled historical reviews bằng heuristic có phiên bản;
  không xác nhận review giả, gian lận hoặc tính xác thực sách.
- Market chỉ tính aggregate cắt ngang trên snapshot; không tạo trend/live-market
  claim.
- JSON và POST-based SSE `/api/v2`, stable errors, durable retry/cancellation và
  provenance. `/api/v1` đã bị gỡ; development `POST /chat` là single-agent fixture.
- Test profile 24 sách/115 review; evaluation profile 200 sách/1.773 review.
- V2 dùng published PostgreSQL corpus/index pin rõ version. Corpus curated có
  20 sources và exact-work mappings cho 20/200 catalog records; không đủ cho
  kiến thức mọi sách. Qdrant/`KNOWLEDGE_BACKEND` là adapter lịch sử.

## Experience mode

Operate. Surface chính là decision workspace tương tác, không phải marketing
page hoặc dashboard thị trường.

## Design principles

1. Evidence before spectacle: kết luận nổi bật phải nằm gần provenance và ranh
   giới snapshot lịch sử.
2. Orchestration made legible: phân biệt deterministic controls, model stages,
   agent work và tool evidence.
3. Dense but calm: đủ chi tiết để audit mà không biến first viewport thành màn
   hình giám sát.
4. Honest resilience: loading, partial success, fallback, cancellation, empty,
   offline và error là first-class states.
5. Vietnamese-first copy: ngắn, chuyên nghiệp, không phóng đại claim. Giữ thuật
   ngữ kỹ thuật bằng English khi bản dịch tiếng Việt trở nên gượng hoặc mơ hồ.

## Accessibility and interaction

- WCAG 2.2 AA contrast target, keyboard operation, visible focus và skip link.
- Focus không bị sticky/floating surfaces che khuất.
- Motion giới hạn ở transition có giá trị và tắt bằng `prefers-reduced-motion`.
- API credentials chỉ nằm trong memory; session identity và conversation không
  nhạy cảm có thể dùng `sessionStorage`.

## Evidence and claim boundary

UI có thể nói repository chứng minh typed orchestration, authorization/fallback
deterministic, structured model calls, provenance và regression evaluation khi
các check tương ứng pass. Model prose không tự trở thành grounded chỉ vì nêu tên
source. Catalog fact text và citation binding thuộc server; knowledge prose có
thể do model viết rồi được exact-span và automated entailment checks kiểm tra.
Không diễn giải guard này thành zero hallucination.

Không được claim current Tiki catalog/price/inventory, marketplace
representativeness, human-preference superiority, semantic-helpfulness
superiority, high availability hoặc “perfect production” khi chưa có bằng chứng
trực tiếp.
