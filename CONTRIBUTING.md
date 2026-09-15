# Contributing và verification

Đóng góp phải giữ ranh giới evidence của repository. Mỗi thay đổi cần mô tả
đường runtime bị ảnh hưởng, dữ liệu đã dùng và các check đã chạy. V1 là
compatibility boundary; v2 dùng PostgreSQL durable state và published
corpus/index. Không gộp một thay đổi v1/v2 vào historical evaluation artifact
nếu không có lý do và binding được review.

## Secrets và dữ liệu

- Không commit, in, hash, gửi cho agent khác hoặc ghi vào artifact bất kỳ API
  key, database password, Redis credential, token, private key, `.env` hay raw
  provider payload nào.
- Chỉ dùng `.env.example` làm template. Secret thật chỉ đi qua environment hoặc
  secret manager ở boundary cần thiết; log và docs phải dùng placeholder.
- Không đưa benchmark question/answer, calibration labels/results, user data,
  raw review text hoặc provider response vào published corpus/index.
- Không sửa tay `app/frontend/dist`, generated checkpoint, frozen protocol,
  split, gold, pricing, corpus/index manifest hoặc captured evidence để làm cho
  một check pass. Muốn thay đổi frozen input phải tạo version/binding mới và
  ghi rõ lý do.

## Frozen artifacts và evaluation

`evaluation/v3/` và P7 artifacts operator-local trong `output/evaluation-v3/pilot/` là đầu vào
đã freeze. Package 8 là additive: giữ nguyên P7 protocol hash
`f91cd3a730f1e8bd61020a4770a568462d8b3729144d0fb728a19ad2736f17d3`, bốn
variants, split 20 development/60 held-out và repeat decision global 3.
Không overwrite output đã có; dùng run ID/output directory mới hoặc `resume`
đúng checkpoint. Partial benchmark luôn phải giữ trạng thái partial.

Lệnh `validate`, `dry-run` và `prepare` của Package 8 không gọi provider.
`prepare` chỉ chấp nhận receipt không có citation; exact citation cần lifecycle
`operate` có authority đã ghi trong receipt. Lệnh live (`run`, `resume`,
`operate`) phải truyền rõ `--allow-network` và `--database-url`; `operate` còn
cần hard limit judge per-job. Không chạy live benchmark để thử nghiệm trong PR.
Held-out result chỉ được báo cáo sau khi đủ 720 cells,
calibrated automated judge, exact evidence resolver, complete judgment join,
analysis/report và final package gates.

## Verification bắt buộc

Trước khi gửi thay đổi, chạy các check phù hợp với phạm vi. Không gọi một check
offline là verification của integration thật.

```powershell
ruff check app tests migrations
ruff format --check app tests migrations
mypy app
pytest -m "not integration" -q
pytest -q tests/test_helm_assets.py tests/test_deployment_assets.py
```

Với thay đổi API, turn lifecycle, action, lease, replay, migration, budget hoặc
knowledge store, phải chạy các test focused tương ứng trên PostgreSQL disposable
thật. SQLite/fake adapter chỉ kiểm tra đường fixture; nó không thay thế
transaction, locking, idempotency, recovery hay durable evidence verification.
Xác nhận migration head từ source là `20260910_0008` trước khi mô tả v2 là đã
verify.

Với thay đổi frontend hoặc transport/UI contract, chạy:

```powershell
Push-Location frontend
npm ci
npm run lint
npm test
npm run build
Pop-Location
```

Khi layout, SSE recovery hoặc interactive state thay đổi, chạy browser QA ở các
viewport liên quan và kiểm tra console/page errors, duplicate IDs, focus,
overflow và terminal/reconnect behavior.

Với v2 knowledge/evidence, xác nhận published snapshot
`books-v1-calibrated-20260909` có 20 sources/chunks/vectors, 200 mappings (20
exact work, 17 ambiguous, 163 unmatched), embedding
`text-embedding-3-small`/1536 dimensions, và benchmark Q/A/calibration vẫn nằm
ngoài corpus. Kiểm tra exact source/version/chunk/span reopening khi code chạm
grounding hoặc citation.

## Package gates

Một package chỉ được đánh dấu complete khi gate của nó đã pass và evidence được
ghi trong `docs/thanh-v2/progress.md`. Tối thiểu phải có:

- focused tests cho thay đổi và regression case nếu có bug;
- full applicable offline suite, lint/format/type checks;
- real PostgreSQL checks cho transactional/durable paths;
- frontend checks khi frontend hoặc transport bị ảnh hưởng;
- migration/deployment/docs link and command verification;
- truthful status: no benchmark quality, held-out result, human judgment or
  production claim from a partial/pilot/development run.

Không reset, xóa volume, reseed production, sửa bảng trực tiếp, gọi provider
hoặc commit artifact frozen để vượt qua gate mà không có yêu cầu và review phù
hợp.
