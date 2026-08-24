# Dữ liệu công khai và provenance

## Snapshot Tiki Books

Pipeline `ecommerce-data` tải version 4 của [Tiki Books Dataset](https://www.kaggle.com/datasets/biminhc/tiki-books-dataset),
giấy phép CC0. Archive được kiểm tra SHA-256 trước khi đọc. Snapshot ứng dụng
chỉ lấy tối đa 200 sản phẩm và 10 review cho mỗi sản phẩm bằng sampling ổn định
theo seed; dữ liệu raw không được commit.

```powershell
ecommerce-data download --output data/raw/tiki-books-v4.zip
ecommerce-data prepare `
  --archive data/raw/tiki-books-v4.zip `
  --output data/snapshots/tiki-books-v4-sample `
  --products 200 `
  --reviews-per-product 10 `
  --seed 42
```

Output gồm `products.jsonl`, `reviews.jsonl` và `manifest.json`. Manifest lưu
source URL, license, source revision, raw archive hash, sampling policy, số dòng
bị loại và snapshot hash. `customer_id` không được đưa vào normalized output vì
không cần cho nghiệp vụ.

Bộ synthetic 30 sản phẩm/150 review vẫn được giữ riêng cho regression test; nó
không được trình bày là dữ liệu thị trường thật. Khi import snapshot công khai,
UI/API phải hiển thị rõ dataset version và việc seller không có trong nguồn nếu
trường này bị thiếu.
