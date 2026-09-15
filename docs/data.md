# Vòng đời dữ liệu Tiki Books

## 1. Phạm vi và nguồn dữ liệu

Runtime hiện tại chỉ dùng snapshot sách lịch sử được làm sạch từ
[Kaggle Tiki Books Dataset](https://www.kaggle.com/datasets/biminhc/tiki-books-dataset),
version 4. Repository pin các thuộc tính sau trong code và manifest:

| Thuộc tính | Giá trị đã pin |
| --- | --- |
| Dataset | `tiki-books` / `kaggle-v4` |
| Source revision | `4` |
| License được nguồn công bố | `CC0-1.0` |
| Thời điểm truy xuất được ghi nhận | `2026-08-24T03:47:30.042026Z` |
| Archive SHA-256 | `371a618ed66425f8f2738ab01514ab2f20ed1bed645258b509fcce443bd32bbb` |
| Source members | `book_data.csv`, `comments.csv` |

Đây là derivative deterministic của archive đã pin, **không phải feed Tiki
trực tiếp**. Nó không phản ánh tồn kho, giá, người bán, review hoặc thị trường
hiện tại. Hai snapshot đã commit đều không có coverage cho người bán và thời
điểm review; không được suy diễn các trường thiếu đó.

## 2. Luồng tái tạo

Yêu cầu Python 3.12+ và package đã được cài từ repository. Verify fixture đã
commit, rồi tái tạo vào thư mục ignored để không ghi đè evidence đang review:

```powershell
ecommerce-data validate --snapshot data/snapshots/tiki-books-v4-eval
ecommerce-data download
ecommerce-data prepare `
  --profile eval `
  --output data/cache/reproduced-tiki-books-v4-eval
ecommerce-data validate `
  --snapshot data/cache/reproduced-tiki-books-v4-eval
```

Giá trị mặc định là:

- archive: `data/raw/tiki-books-v4.zip`;
- output: `data/snapshots/tiki-books-v4-<profile>`;
- sampling seed nội bộ: `42`.

Có thể chỉ định đường dẫn rõ ràng:

```powershell
ecommerce-data download --output data/raw/tiki-books-v4.zip
ecommerce-data prepare `
  --archive data/raw/tiki-books-v4.zip `
  --profile test `
  --output data/cache/reproduced-tiki-books-v4-test
```

`prepare` từ chối ghi đè thư mục output đã tồn tại. `--force` chỉ nên dùng khi
chủ động tái tạo đúng snapshot từ archive đã kiểm tra; pipeline vẫn không được
ghi vào thư mục ngoài output đã chỉ định.

Luồng thực thi là:

```text
download atomically
  → verify archive SHA-256 và CSV contract
  → clean/deduplicate toàn bộ source rows
  → redact URL/email/phone và normalize schema
  → chọn profile sau cleaning
  → ghi bốn artifact
  → quality gate
  → revalidate schema/count/hash
  → import transactionally
```

Archive mới được ghi qua file `.part` cùng thư mục rồi đổi tên sau khi checksum
đúng. Archive đã tồn tại được tái sử dụng chỉ khi checksum khớp; checksum sai bị
từ chối trước khi mở ZIP. Pipeline yêu cầu đúng một `book_data.csv` và một
`comments.csv` với header contract đã pin.

## 3. Profiles

| Profile | Chính sách deterministic | Snapshot đã commit |
| --- | --- | ---: |
| `test` | 24 sách bắt buộc; tối đa 5 review/sách | 24 sách, 115 review |
| `eval` | 200 sách; tối đa 10 review/sách; tổng 1.773 review | 200 sách, 1.773 review |
| `full` | Toàn bộ record hợp lệ sau cleaning; không sampling | Không commit |

Con số review theo sách là giới hạn trên, không phải quota phải lấp đầy; vì vậy
profile `test` có 115 thay vì 120 review. Product selection dùng stable SHA-256
ranking kết hợp category round-robin; review selection được phân tầng theo
rating. Mọi profile đều được chọn **sau** khi cùng một nguồn đầy đủ đã được làm
sạch, nên input/drop/redaction counters mô tả nguồn đầy đủ còn output coverage
mô tả profile đã chọn.

`data/raw/`, `data/cache/` và `data/snapshots/tiki-books-v4-full/` bị ignore.
Profile `test` và `eval`, bao gồm manifest/quality report, được commit để test,
evaluation và image build có đầu vào tái lập.

## 4. Artifact contract

Mỗi snapshot hợp lệ có đúng bốn artifact bắt buộc:

| File | Nội dung |
| --- | --- |
| `products.jsonl` | Book facts đã normalize, một JSON object mỗi dòng |
| `reviews.jsonl` | Review đã normalize/redact, một JSON object mỗi dòng |
| `manifest.json` | Source/version/profile, policy, counts và artifact hashes |
| `quality-report.json` | Input/output, correction/drop/redaction, coverage và gate result |

`customer_id` không được đưa vào normalized review. URL, email và số điện thoại
trong review được thay bằng token `[URL]`, `[EMAIL]`, `[PHONE]`. Manifest không
chứa row content; quality report chỉ chứa aggregate counters.

Quality gate fail closed khi phát hiện một trong các trạng thái như:

- product/review external ID trùng hoặc review mồ côi;
- record không thuộc platform Tiki;
- pattern URL/email/phone còn sót trong review;
- thiếu product ID bắt buộc của profile;
- count, schema, profile target hoặc hash không khớp;
- manifest và quality report không cùng provenance;
- thiếu bất kỳ artifact nào hoặc report không có `status=pass`.

`ecommerce-data validate` đọc lại toàn bộ schema, count và hash byte-for-byte;
không chỉ tin vào cờ `status` trong report.

## 5. Import và bootstrap

Chạy migration rồi chọn **một** trong hai đường import:

```powershell
ecommerce-migrate

# Bootstrap nghiêm ngặt cho database trống hoặc đã chứa đúng snapshot này.
ecommerce-seed --snapshot data/snapshots/tiki-books-v4-eval

# Hoặc import trực tiếp một profile đã validate.
ecommerce-data import --snapshot data/snapshots/tiki-books-v4-eval
```

`ecommerce-seed` mặc định đọc `PUBLIC_SNAPSHOT_DIR`, hiện là
`data/snapshots/tiki-books-v4-eval`. Compose đóng gói đường dẫn tương ứng trong
container và chạy `migrate → seed-data → backend`.

Importer validate trước khi ghi, stream trong transaction và validate lại sau
khi đọc để phát hiện artifact bị thay đổi giữa chừng. Identity bất biến là
`(dataset_id, dataset_version, profile)`. Import lại đúng snapshot là
idempotent; source metadata xung đột, rows khác hash/count, database trộn profile
hoặc row không có provenance đều bị từ chối.

Database lưu metadata ở `dataset_sources`; facts trả về dùng provenance dạng
`tiki-books:kaggle-v4:<profile>`, ví dụ `tiki-books:kaggle-v4:eval`, và vẫn gắn
`sample_data=true` vì đây là một mẫu lịch sử chứ không phải feed hiện tại.

Migration schema production hiện hành là `20260910_0008`, khớp
`app.db.migrate.EXPECTED_DATABASE_REVISION`. Các bảng `v2_` bổ sung hội thoại,
sandbox, knowledge có phiên bản, runtime metadata và ledger ngân sách; không
thay semantics dữ liệu snapshot v1. Xem [foundation v2](thanh-v2/package-2-foundation.md).

## 6. Published corpus/index cho API v2

API v2 dùng durable PostgreSQL cho conversation, turn, action, usage ledger và
knowledge store. Runtime chỉ resolve corpus/index đã publish và pin hai identity
này vào mỗi turn; không lấy benchmark fixture làm knowledge source.

Snapshot hiện hành là `books-v1-calibrated-20260909`:

| Thuộc tính | Giá trị đã xác minh |
| --- | --- |
| Corpus ID | `cor_e06f6abbf338cfcf5fe17d450eec52ed961975e0fa8ee6bae32369ed3956` |
| Index ID | `idx_69af0802b50991c371bcb1f2954e79de82ccdc7855e15d3e602126b3b9c4` |
| Sources / chunks / vectors | 20 / 20 / 20 |
| Product mappings | 200: 20 exact work, 17 ambiguous, 163 unmatched |
| Embedding | `text-embedding-3-small`, 1.536 dimensions |
| Chunker / enrichment | `table_chunker_v1` / `source_keywords_v1` |

Corpus gồm các source notes được curate, chunks, vectors và mapping/provenance
records. Benchmark questions, answers, development probes, calibration labels
và calibration outputs bị loại khỏi corpus/index; chúng chỉ là evaluation
inputs hoặc verification artifacts. `ambiguous` và `unmatched` mappings không
được nâng thành exact evidence trong runtime.

Publication is immutable: v2 phải resolve đúng corpus/index đã publish, kiểm tra
đủ vector count và khớp embedding model/dimension trước retrieval. Rebuild với
fingerprint khác tạo index identity mới; không overwrite index đang được pin.

## 7. Dữ liệu legacy

Seed tổng hợp cũ 5 shop/30 sản phẩm/150 review không còn là bootstrap runtime hay
đầu vào test/evaluation hiện hành. Một số artifact evaluation v1 generic vẫn
được giữ trong thư mục `legacy-*` để audit lịch sử. Chỉ xóa seed cũ khỏi một
database disposable bằng lệnh guard riêng:

```powershell
ecommerce-reset-legacy-seed `
  --confirm-disposable DELETE-LEGACY-SEED
```

Không dùng lệnh này để reset database có dữ liệu cần giữ. Snapshot public mới
phải luôn đi qua download/prepare/validate/import ở trên; không chèn JSONL hoặc
row SQL thủ công để bỏ qua quality gate.
