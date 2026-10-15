# P7/P8 r3 — kết quả phát triển, 2026-10-12

Gói này lưu raw checkpoint, protocol, schedule, runner và reference packet phát triển cho P7/P8 r3 trên `app/v2`. Đây là regression run trên held-out đã được dùng trong remediation; **không phải tập xác nhận độc lập** và chưa có nhãn ngữ nghĩa từ LLM hay người chấm.

- SUT commit: `e99f186eb6662a99cd8397da1ea633473a6b3a54`.
- P7: 36/36 cells; P8: 720/720 cells (60 ca × 4 nhánh × 3 lần lặp). Receipt có 864 lượt hội thoại; outcome dùng lượt cuối, latency E2E cộng thời gian các lượt trong ca.
- P8 ghi `$4.27855988` known cost; 3.108.871 input+output tokens; reservation chưa quyết toán `$0`.
- ZIP: [`p7-p8-r3-raw-artifacts.zip`](p7-p8-r3-raw-artifacts.zip), SHA-256 trong [`manifest.json`](manifest.json). Số liệu trích từ raw checkpoint trong [`summary.json`](summary.json).

| Nhánh | Answered ở lượt cuối / 180 | E2E p50 / p95 (giây) | Chi phí | Tokens
| --- | ---: | ---: | ---: | ---: |
| Single-Agent + RAG | 120 | 11,484 / 25,562 | $1,2867 | 908.330 |
| Fixed Multi-Agent + RAG | 120 | 9,508 / 28,500 | $1,3089 | 965.040 |
| Adaptive Multi-Agent + RAG | 119 | 10,820 / 28,000 | $1,3608 | 989.113 |
| Adaptive Multi-Agent không RAG | 0 | 1,649 / 8,047 | $0,3221 | 246.388 |

`answered` là trạng thái ứng dụng ở lượt cuối, không phải độ đúng. Adaptive RAG có thêm model calls/cell so với Fixed/SA và p95 cao hơn SA; chưa có judge để xác định chất lượng tương ứng. Trong cả 864 lượt người dùng không có revision kế hoạch bổ sung, nên continuation chưa được exercised bởi workload này.

## Nội dung và tái lập

Zip gồm P7 protocol/schedule/checkpoint/binding/repeat decision, P8 execution plan/schedule/checkpoint, runners, reference packet 32 ca **development calibration**, runtime manifest và summary. Reference packet được đặt tên `development-reference-cases.p8.json` để không nhầm là oracle của challenge set độc lập. Không đưa `.env`, API key hoặc credential vào gói.

Checkout commit nêu trên, cài dependencies của repo, cấu hình API key/PostgreSQL, khôi phục snapshot catalog/corpus/index có hash trong protocol/runtime manifest, rồi chạy runner tương ứng từ project root. Model sinh: `gpt-5.4-mini-2026-03-17`; embedding: `text-embedding-3-small`. Tái chạy live tạo chi phí API. Gói lưu đầy đủ input/output/trace để kiểm tra offline nhưng không chứa bản dump PostgreSQL/vector; phải dựng lại DB từ source snapshot đã pin và xác minh hash trước khi gọi runner.

`720/720` chỉ chứng minh coverage. Các raw outcomes, warnings, latency, token/cost và giới hạn trace nằm trong `summary.json` cùng [`docs/evaluation.md`](../../../docs/evaluation.md). LLM judge/operate chưa chạy; không dùng kết quả này để khẳng định MA vượt SA.
