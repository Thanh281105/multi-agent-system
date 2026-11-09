# LLM Judge Re-judge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chấm lại 144 câu trả lời v4 (có nguồn snapshot) và 720 câu trả lời P8 (test-retest) bằng `gpt-6-luna`, đo độ đồng thuận và tính lại so sánh ghép cặp, với trần chi phí ép cứng và không gọi API trước khi người dùng duyệt ngân sách.

**Architecture:** Tách pha mạng và pha chấm. Pha mạng: gọi Luna đồng thời (semaphore), ghi cache JSONL theo khóa ổn định (resume được, không gọi lại ô đã có kể cả ô lỗi). Pha chấm: B2 dùng `judge_blinded_packet_v3` với callable đọc cache (không mạng); B1 dùng schema verdict riêng. Thống kê (đồng thuận, kappa, delta ghép cặp, bootstrap theo work group) là module thuần, test tổng hợp và hồi quy với `analysis.json` gốc.

**Tech Stack:** Python 3.12, pytest, ruff, mypy, `OpenAIModelRuntime` (`app/shared/model_runtime.py`), `app/evaluation/v3_judge.py`, `app/shared/pricing-manifest.json`, `decimal.Decimal`.

**Spec:** `docs/superpowers/specs/2026-11-09-llm-judge-rejudge-design.md`

## Global Constraints

- Model judge: `gpt-6-luna`; giá lấy từ `app/shared/pricing-manifest.json` (`models["gpt-6-luna"]`: input $0,10/M, cached $0,01/M, output $0,50/M); thiếu mục giá → dừng, không tự thêm giá.
- Không gọi mạng trong test; model thay bằng fake.
- Không sửa artifact frozen/manifest cũ; đầu ra vào thư mục mới `output/evaluation-v3/rejudge-20261109/` (gitignored) kèm manifest hash.
- Không chỉnh số liệu hay nhãn; kết quả bất lợi cho MA vẫn báo cáo.
- Output lỗi parse/validate được lưu mã lỗi, **không tự gọi lại**.
- API thật chỉ chạy sau khi người dùng duyệt trần ngân sách bằng số cụ thể; trần ép trong code (`--budget-usd`), dừng ngay khi vượt.
- Giới hạn trung thực phải ghi trong mọi báo cáo: cùng model không phát hiện thiên lệch chung; B2 chỉ đo độ ồn; judge chưa người hiệu chuẩn; v4 là thăm dò (thiết kế sau khi xem lỗi P8); judge có căn cứ không được "cứu" Sapiens (blueprint yêu cầu đúng tên "(Tái Bản 2022)").
- Commit local nguyên tử, Conventional Commits, kết thúc `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`; không push; không đụng `graphify-out/`, `report.pdf`.
- Sau khi sửa code chạy `graphify update .`. Trả lời người dùng bằng tiếng Việt.
- Mã mới nằm trong `app/evaluation/rejudge_*.py`; test phẳng trong `tests/test_rejudge_*.py` (khớp bố cục hiện có).

## Review Focus

1. Ô đã có trong cache (kể cả lỗi) không bị gọi lại khi `run` lần hai → test `test_resume_skips_cached_cells`.
2. Vượt trần chi phí giữa chừng: dừng trước khi gọi ô kế tiếp, cache đã ghi vẫn dùng được → test `test_budget_cap_stops_before_next_call`.
3. Kappa suy biến (một nhãn duy nhất ở cả hai lần chấm, mẫu rỗng) không chia cho 0 → test `test_kappa_degenerate_returns_none`.
4. Đầu ra Luna sai schema/JSON hỏng được ghi mã lỗi, không làm sập cả lượt và không retry → test `test_invalid_output_recorded_not_retried`.
5. Fact có `source_type` không có `artifact_path` (`guardrail_policy`, `synthesis_recommendation`) vẫn dựng được ngữ cảnh → test `test_context_for_policy_fact_without_artifact`.

---

### Task 1: Module thống kê đồng thuận và so sánh ghép cặp

**Files:**
- Create: `app/evaluation/rejudge_stats.py`
- Test: `tests/test_rejudge_stats.py`

**Interfaces:**
- Produces:
  - `agreement(a: Sequence[object], b: Sequence[object]) -> float | None` (tỷ lệ trùng; `None` nếu rỗng)
  - `cohens_kappa(a: Sequence[object], b: Sequence[object]) -> float | None` (`None` khi pe == 1 hoặc rỗng)
  - `paired_variant_delta(scores: Mapping[tuple[str, str], float], *, baseline: str, treatment: str, seed: int = 42, resamples: int = 10_000) -> PairedDelta` với khóa `(work_group_id, variant_id)` là điểm trung bình theo nhóm; `PairedDelta(mean_delta: float, ci_low: float, ci_high: float, wins: int, ties: int, losses: int)`; bootstrap theo work group.

- [ ] **Step 1: Viết test lỗi** — `test_agreement_basic` (`[1,0,1,1]` vs `[1,0,0,1]` → 0.75); `test_kappa_known_value` (bảng 2×2 cho kappa = 0.4 tính tay); `test_kappa_degenerate_returns_none`; `test_paired_delta_wtl_and_mean` (3 nhóm: +0.2, 0, −0.1 → mean 0.0333…, W/T/L = 1/1/1); `test_paired_delta_deterministic_seed` (cùng seed → cùng CI).
- [ ] **Step 2: Chạy** `.venv/Scripts/python.exe -m pytest tests/test_rejudge_stats.py -v` → FAIL (module chưa có).
- [ ] **Step 3: Cài đặt** các hàm trên bằng thư viện chuẩn (`random.Random(seed)`, không thêm dependency).
- [ ] **Step 4: Chạy lại** → PASS; `ruff check app/evaluation/rejudge_stats.py tests/test_rejudge_stats.py` và `mypy app/evaluation/rejudge_stats.py` sạch.
- [ ] **Step 5: Commit** `feat(evaluation): thêm module thống kê đồng thuận cho chấm lại bằng LLM judge`.

### Task 2: Cost guard từ pricing manifest

**Files:**
- Create: `app/evaluation/rejudge_cost.py`
- Test: `tests/test_rejudge_cost.py`

**Interfaces:**
- Produces:
  - `load_model_prices(manifest_path: Path, model: str) -> ModelPrices` (`ModelPrices(input_per_m: Decimal, cached_input_per_m: Decimal, output_per_m: Decimal)`; `KeyError`-tương đương `RejudgeCostError` nếu thiếu model)
  - `call_cost_usd(prices: ModelPrices, *, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> Decimal` (phần cached tính giá cached, phần còn lại giá input)
  - `class BudgetGuard(limit_usd: Decimal)` với `.charge(cost: Decimal) -> None`, `.spent -> Decimal`, `.would_exceed(next_max_cost: Decimal) -> bool`

- [ ] **Step 1: Viết test lỗi** — giá từ manifest thật `app/shared/pricing-manifest.json` đúng ($0,10/$0,01/$0,50 mỗi triệu) (`test_load_luna_prices_from_repo_manifest`); `test_call_cost_splits_cached` (1M input với 400k cached + 100k output → 0,06+0,004+0,05 = $0,114); `test_missing_model_raises`; `test_budget_cap_stops_before_next_call` (mức guard: `would_exceed` true khi spent + next > limit).
- [ ] **Step 2: Chạy** → FAIL.
- [ ] **Step 3: Cài đặt** đọc JSON bằng `Decimal(str(x))`; không làm tròn sớm.
- [ ] **Step 4: Chạy lại** → PASS; ruff/mypy sạch.
- [ ] **Step 5: Commit** `feat(evaluation): thêm cost guard cho chấm lại bằng LLM judge`.

### Task 3: Bộ thu thập phán quyết có cache, resume và trần chi phí

**Files:**
- Create: `app/evaluation/rejudge_collect.py`
- Test: `tests/test_rejudge_collect.py`

**Interfaces:**
- Consumes: `BudgetGuard`, `call_cost_usd`, `ModelPrices` (Task 2).
- Produces:
  - `class JudgeCall(Protocol)`: `async def __call__(self, key: str, request: JudgeRequest) -> JudgeCallResult`
  - `JudgeRequest(instructions: str, input_text: str, schema: type, max_output_tokens: int)`; `JudgeCallResult(output: dict | None, error: str | None, input_tokens: int, cached_input_tokens: int, output_tokens: int)`
  - `async def collect(requests: Mapping[str, JudgeRequest], *, call: JudgeCall, cache_path: Path, guard: BudgetGuard, prices: ModelPrices, concurrency: int = 4) -> CollectSummary` (ghi từng dòng JSONL `{key, output, error, tokens, cost_usd}` ngay khi xong; bỏ qua khóa đã có; dừng nhận ô mới khi `guard.would_exceed(upper_bound)`; `CollectSummary(done: int, skipped_cached: int, errors: int, stopped_by_budget: bool, spent_usd: Decimal)`)
  - `def make_openai_call(model: str, reasoning_effort: str | None) -> JudgeCall` bọc `OpenAIModelRuntime.generate_structured(stage="evaluation.judge", agent_id="model_judge", …)` và đọc token từ `collect_model_calls()`; **không** dùng ledger Postgres.

- [ ] **Step 1: Viết test lỗi** với `call` giả: `test_resume_skips_cached_cells` (chạy hai lần, lần hai gọi 0 lần); `test_budget_cap_stops_before_next_call`; `test_invalid_output_recorded_not_retried` (call trả `error="schema"` → ghi dòng lỗi, đúng 1 lần gọi); `test_cache_line_written_incrementally` (hủy giữa chừng vẫn còn dòng đã xong).
- [ ] **Step 2: Chạy** → FAIL.
- [ ] **Step 3: Cài đặt** `collect` bằng `asyncio.Semaphore`; ghi dưới `asyncio.Lock`; cận trên chi phí một ô = `max_output_tokens` × giá output + ước lượng input (ký tự/3) × giá input. Đọc `OpenAIModelRuntime`/`collect_model_calls` trước khi viết `make_openai_call` (kiểm tra chữ ký thật; không đoán).
- [ ] **Step 4: Chạy lại** → PASS; ruff/mypy sạch.
- [ ] **Step 5: Commit** `feat(evaluation): thêm bộ thu thập phán quyết LLM judge có cache và trần chi phí`.

### Task 4: Chấm lại P8 (B2)

**Files:**
- Create: `app/evaluation/rejudge_p8.py`
- Test: `tests/test_rejudge_p8.py`

**Interfaces:**
- Consumes: `collect`, `JudgeRequest` (Task 3); `agreement`, `cohens_kappa`, `paired_variant_delta` (Task 1); `judge_blinded_packet_v3`, `_serialize_model_judge_input` (đọc chữ ký thật trước), `model_judge_output_type_v3`.
- Produces:
  - `load_p8_artifacts(p8_root: Path) -> P8Artifacts` (blinded_answers, judgments gốc, unblinding_key, configuration, calibration, bindings từ `p8-preparation/` + `p8-publication/scored-artifacts/`)
  - `build_requests(artifacts: P8Artifacts, *, sample: Sequence[str] | None = None) -> dict[str, JudgeRequest]` (khóa = `opaque_answer_id`)
  - `rejudge_scores(artifacts, cache_path) -> dict[str, dict]` (gọi `judge_blinded_packet_v3` với callable đọc cache, không mạng)
  - `compare_runs(artifacts, new_scores) -> dict` (agreement + kappa theo từng nhãn rời rạc/metric; `paired_variant_delta` SA vs MA adaptive cho cả hai lần chấm)

- [ ] **Step 1: Viết test lỗi** — fixture tổng hợp nhỏ (4 answers); `test_build_requests_keys_are_opaque_ids`; `test_rejudge_uses_cache_without_network` (callable cache; xóa cache một khóa → lỗi rõ ràng, không im lặng); `test_regression_original_delta_matches_analysis` — `skipif` thiếu `output/evaluation-v3/heldout-mini-luna-judge-v3-rating-fix-20261014/...` (lý do ghi rõ): tái tạo `mean_delta` task_completion SA→MA adaptive = −0.038065843621399184 từ `judgments.json` gốc + `unblinding_key.json`.
- [ ] **Step 2: Chạy** → FAIL.
- [ ] **Step 3: Cài đặt** như trên; wire input dùng `_serialize_model_judge_input` đúng như P8; `max_output_tokens` 3600 nếu `rubric_context.evaluator_contract` khác None, ngược lại 1200.
- [ ] **Step 4: Chạy lại** → PASS (regression test chạy thật vì dữ liệu có trên máy); ruff/mypy sạch.
- [ ] **Step 5: Commit** `feat(evaluation): thêm chấm lại test-retest cho gói P8`.

### Task 5: Chấm lại v4 có nguồn (B1)

**Files:**
- Create: `app/evaluation/rejudge_v4.py`
- Test: `tests/test_rejudge_v4.py`

**Interfaces:**
- Consumes: `collect`, `JudgeRequest` (Task 3).
- Produces:
  - `build_fact_context(fact: Mapping, snapshot_dir: Path) -> str` — dựng theo `source_provenance.source_type`: `catalog_product` → bản ghi `products.jsonl` có `external_id == record_id`; `customer_review` → dòng `reviews.jsonl` có `external_id == record_id`; `knowledge_excerpt`/`catalog_and_knowledge`/`cross_domain_synthesis` → `exact_quote`/`exact_value` + bản ghi liên quan nếu có; `guardrail_policy` → `policy_rule`; `synthesis_recommendation` → `exact_value`. Không có bản ghi → ghi rõ "không tìm thấy bản ghi" (không bịa).
  - `V4Verdict` (pydantic): `fact_id`, `supported: bool`, `reason: str`; `V4Judgment`: `verdicts: list[V4Verdict]`, `unsupported_extra_claims: list[str]`.
  - `build_requests(results_path: Path, cases_path: Path, snapshot_dir: Path, *, case_ids: Sequence[str] | None = None) -> dict[str, JudgeRequest]` (khóa `case_id|variant|repeat`)
  - `summarize(results_path, cache_path) -> dict` (điểm mới vs điểm cũ theo ca/hệ thống, các ca thay đổi, `unsupported_extra_claims` tổng hợp; W/T/L và delta SA vs MA ghép cặp theo ca)
  - Prompt yêu cầu: fact đạt nếu có mặt đúng nghĩa **và đúng giá trị blueprint** (tên sách phải đúng chuỗi có "(Tái Bản 2022)"); thông tin thêm có trong snapshot không bị phạt; thông tin thêm không có căn cứ → `unsupported_extra_claims`.

- [ ] **Step 1: Viết test lỗi** — `test_context_catalog_record_found`; `test_context_for_policy_fact_without_artifact`; `test_context_missing_record_is_explicit`; `test_build_requests_count_is_144_on_real_results` (`skipif` thiếu `output/evaluation-v4/full_24_benchmark_results.json`); `test_prompt_demands_exact_blueprint_value` (prompt chứa yêu cầu giữ đúng giá trị, kiểm tra theo chuỗi); `test_summarize_flags_changed_cases` (fixture nhỏ).
- [ ] **Step 2: Chạy** → FAIL.
- [ ] **Step 3: Cài đặt** như trên; đọc cấu trúc thật của `full_24_benchmark_results.json` trước (list 24 ca, `single_agent`/`multi_agent.repeats[].answer, score, verdicts`).
- [ ] **Step 4: Chạy lại** → PASS; ruff/mypy sạch.
- [ ] **Step 5: Commit** `feat(evaluation): thêm chấm lại bộ v4 có cấp nguồn snapshot`.

### Task 6: CLI, dry-run offline và manifest

**Files:**
- Create: `app/evaluation/rejudge_cli.py`
- Test: `tests/test_rejudge_cli.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces: `python -m app.evaluation.rejudge_cli {dry-run|sample|run|report} {p8|v4} --out DIR [--n N] [--budget-usd D]`:
  - `dry-run`: dựng toàn bộ request thật, đếm ô, ước lượng token (ký tự/3) và **chi phí tối đa** theo giá manifest; `network_calls: 0`; in JSON.
  - `sample --n N`: chọn mẫu xác định (seed 42; v4 luôn gồm `fair_tier2_cross_06/07`), gọi API, yêu cầu `--budget-usd`.
  - `run`: chạy đủ, **từ chối chạy nếu thiếu `--budget-usd`**; resume từ cache.
  - `report`: offline, sinh bảng đồng thuận/so sánh + `manifest.json` (sha256 mọi file, git HEAD, model, hash prompt, tổng token/chi phí).
  - Cổng an toàn: thiếu `OPENAI_API_KEY` → lỗi trước khi gọi bất kỳ ô nào; lệnh `sample/run` không chạy khi chưa có cờ `--allow-network`.

- [ ] **Step 1: Viết test lỗi** — `test_dry_run_makes_no_network_calls` (call giả đếm = 0); `test_run_requires_budget`; `test_run_requires_allow_network`; `test_missing_api_key_fails_before_any_call`; `test_report_writes_manifest_with_hashes`.
- [ ] **Step 2: Chạy** → FAIL.
- [ ] **Step 3: Cài đặt** dùng `argparse`, khớp phong cách `v3_cli.py` (đọc trước).
- [ ] **Step 4: Chạy lại** → PASS; ruff/mypy sạch.
- [ ] **Step 5: Commit** `feat(evaluation): thêm CLI chấm lại bằng LLM judge với dry-run và manifest`.

### Task 7: Cổng offline đầy đủ (không tốn tiền)

**Files:** không sửa mã (chỉ sửa nếu phát hiện lỗi, kèm test).

- [ ] **Step 1:** `ruff check .`, `mypy app`, `pytest` đầy đủ với Postgres tạm (Docker `ea-test-pg`, cổng 55433, `--basetemp` trong scratchpad, `MSYS_NO_PATHCONV=1`) → 0 lỗi, 0 fail (số test = 1.418 + test mới).
- [ ] **Step 2:** `rejudge_cli dry-run p8` và `dry-run v4` trên dữ liệu thật; ghi số ô, token ước lượng, **chi phí tối đa**; xác nhận `network_calls: 0` và 720 / 144 ô.
- [ ] **Step 3:** `graphify update .`; `git status` sạch trừ `graphify-out/`, `report.pdf`.
- [ ] **Step 4: DỪNG — báo người dùng kết quả preflight và xin duyệt trần ngân sách bằng số cụ thể (đề xuất: mẫu 24 ô ≤ $0,10; B1 ≤ $1; B2 ≤ $2).** Không chạy tiếp khi chưa có số.

### Task 8: Chạy API có kiểm soát (chỉ sau khi người dùng duyệt)

**Files:** đầu ra trong `output/evaluation-v3/rejudge-20261109/` (gitignored).

- [ ] **Step 1:** `sample` v4 24 ô (gồm `cross_06/07`) với trần đã duyệt. Kiểm tra: tỷ lệ lỗi parse ≈ 0, chi phí thực tế gần ước lượng (lệch > 2× → dừng và báo), key/hạn mức/rate limit ổn.
- [ ] **Step 2:** `sample` P8 12 ô tương tự. Nếu ổn → báo người dùng, rồi `run` đủ B1 (144) rồi B2 (720) với trần đã duyệt; lỗi giữa chừng → `resume`, không chạy lại từ đầu.
- [ ] **Step 3:** `report` cho cả hai; chạy `evidence_integrity` cho manifest mới → valid.

### Task 9: Tích hợp kết quả vào tài liệu và báo cáo

**Files:**
- Modify: `docs/defense-qa.md`, `latex/03-danh-gia.tex`
- Modify (nếu người dùng đồng ý cách nói): `README.md` đoạn trạng thái bằng chứng, `docs/p8_official_publication_benchmark_report.md` (nhãn ablation like-for-like)

- [ ] **Step 1:** Thêm mục kết quả B1/B2 với đúng số đo được: bảng điểm v4 trước/sau cấp nguồn, đồng thuận + kappa, delta ghép cặp SA vs MA adaptive hai lần chấm, chi phí thực tế; nêu đầy đủ giới hạn trung thực; chỉ dùng cụm "kết luận ổn định/không ổn định qua hai lần chấm bằng cùng judge", không viết "judge đã được kiểm chứng".
- [ ] **Step 2:** Biên dịch lại PDF (`latex/Build.ps1`) exit 0; kiểm tra trang mới bằng cách đọc log và (nếu có) xem trang.
- [ ] **Step 3:** Commit tài liệu (`docs(report): thêm kết quả chấm lại bằng LLM judge`); báo cáo git cuối task (số commit, hash, thay đổi chưa commit).
