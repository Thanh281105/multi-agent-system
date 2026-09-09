# thanh-v2 execution progress

Implementation contract: [approved plan](implementation-contract.md). Work packages run sequentially; the next package starts only after the current gate is met. Following the user's updated instruction, independently reviewed and tested slices are committed as they finish, using [commit.md](../../commit.md), with a final gate record for each package. Package 1 records baseline failures as required by its gate; subsequent gates require their named tests to pass.

| Package | State | Gate / evidence |
| --- | --- | --- |
| 1 — branch, environment, baseline, provenance | Committed `6b046d4` | Baseline and environment status established; tests, local database and attribution reviewed. Known captured-artifact hash failure recorded below. |
| 2 — contracts, registry, schema, authorization, budget | Committed `16f0d2a` | 395 tests pass; contracts, real PostgreSQL, exact-cost ledger and v1 regression verified. Seven implementation/fix commits plus the gate record. |
| 3 — corpus, mapping, retrieval, citations | In progress | Three disjoint source/ingestion/retrieval scopes; runtime database and license prerequisite ready. |
| 4 — supervisor, continuation, deduplication, grounding | Not started | Bounded end-to-end reads; no duplicate completed step |
| 5 — history, memory, sandbox actions | Not started | Restart/retry/confirmation/real PostgreSQL concurrency |
| 6 — API and frontend | Not started | JSON/SSE/UI consistency; v1 regression |
| 7 — evaluation v3, gold, pilot, freeze | Not started | Budget projection and immutable protocol |
| 8 — benchmark, error analysis, final checks/docs | Not started | Full acceptance checklist; honest completeness state |

## Execution rules

- Lead owns review, integration, gates and commits. Sol XHigh implements bounded tasks; Sol Max is reserved for architecture, hard debugging, transactional concurrency/idempotency, supervisor and grounding.
- Explicit file ownership precedes concurrent edits; source snapshot/review data and credentials are not delegated. `Multi-Agent-lac-v2` is read-only, including its pre-existing `.gitignore` change.
- v1 schema/routing/SSE and captured evaluation v1/v2 artifacts remain regression boundaries.
- PostgreSQL is authoritative for durable v2 state, action execution, replay and reservations. No provider/network calls inside action transactions.
- No live provider baseline or ingestion calls before the attempt ledger is ready. Offline regression does not establish live model quality or replace PostgreSQL integration.
- Restart/resume uses this progress document, committed package boundaries and recorded observations; it must not replay completed steps or benchmark observations.

## Package 1 change budget

Expected tracked changes: the verbatim implementation contract, this progress record, selective-integration provenance, and a loopback-only local PostgreSQL/Redis Compose file. No application behavior changes. Environment installs and raw test logs remain ignored.

## Environment findings

- Workspace parent `DA_CNTT` is not a Git repository; the target is its `Multi-Agent` child.
- Repository preflight: clean `thanh-v1`, own `.codegraph/`, sensitive `.env`/`.env.example` excluded from delegation.
- Sandbox PATH resolves Python to Cygwin and hides native Python; native Python 3.12.10 is available outside sandbox. A dedicated `.venv` was installed from committed requirements locks; editable package installation passed. Native Python execution also requires the approved elevated context. Windows resolves the transitive `tzdata` dependency to `2026.3`; the existing lock does not pin this platform dependency.
- Docker Desktop/Engine is running (Engine 28.0.4); sandbox cannot open its named pipe. Approved elevated execution reaches it. No daemon failure or unavailable PostgreSQL has been inferred from sandbox denial.
- Existing source repo checked with command-scoped `safe.directory`, without modifying global Git settings.

## Outstanding verification

Package 1 gate is met: baseline status and environment failures are known. The lead reviewed the provenance source map against the pinned source, reproduced PostgreSQL two-pass seed idempotency, inspected test logs and verified generated wheel contents. Live provider quality, deployment checks and later-package acceptance remain unverified. No benchmark has been run.


## Baseline results (base b7df1cc)

| Check | Outcome |
| --- | --- |
| Python | Native CPython 3.12.10, committed requirements installed; editable package built |
| Ruff lint | PASS, `ruff check app tests migrations scripts` |
| Ruff format | PASS, 169 files already formatted |
| mypy | PASS, 120 source files |
| Offline pytest | PASS, 303 passed; one optional live-provider test module skipped; one FastAPI/Starlette TestClient deprecation warning; 175.34 seconds |
| Frontend install | Node 24.19.0 / npm 11.17.0; `npm ci` passed with unchanged lock; audit reports 2 moderate and 1 high issue (not modified in baseline) |
| Frontend checks | PASS lint; 5 Vitest files / 47 tests, no skipped tests; production build 2,017 modules |
| Legacy captured single-agent re-score | PASS, resulting tracked artifacts unchanged |
| Legacy captured Multi-Agent re-score | Scoring exits 0 and metrics unchanged; artifact immutability check FAILS because the runner replaces historical `sut_source_sha256` with current checkout hash |

The captured Multi-Agent report is preserved exactly after the experiment. Its saved hash is `c02f2550d867eaa30ad06750c8379a7a09733c61b945cf61de87708992ff5437`; baseline source hashes to `213c2b50d32ad42be33933fcb0d861f62d864e922226943d950e013bf6c2c0e0`. `scripts/run_real_multi_agent_benchmark.py` computes the source manifest from current `app/**/*.py` while re-scoring. Hashing already normalizes CRLF, so this is not attributed to Windows line endings. This pre-existing reproducibility failure is recorded rather than changing historical artifacts or calling it a pass. New evaluation v3 must bind captures to their frozen manifest.

No baseline provider calls, embedding requests, warmup, judge calls or benchmark observations were sent.


## Local baseline services

`deploy/compose.local.yaml` starts a separate, loopback-only project `thanh-v2-p1-services`; no existing containers or volumes were modified. PostgreSQL 16.15 and Redis 8.10.0 are healthy. PostgreSQL is at `127.0.0.1:55432`, database/user `ecommerce_p1`; Redis is at `127.0.0.1:56379`. Both use the documented disposable local password `p1-local-disposable-only`, not production credentials.

The lead independently queried PostgreSQL: Alembic `20260830_0003`, 200 products, 1,773 reviews. The migration/schema checks passed; Redis returned `PONG`. Detailed two-pass seed evidence is retained in the package service report.

Python wheel build passed and the lead verified `index.html`, favicon, JavaScript, CSS and font assets in the wheel (`WHEEL_FRONTEND_STATUS=PASS`).

The global documentation verifier reports `VERIFY_STATUS=SUCCESS`, with `VERIFY_SKIPPED=docs-link-check` and `VERIFY_TESTS=not-run`. This is a whitespace/configured documentation check only; the explicit baseline commands above establish application verification.


## Package 1 gate decision

Gate met on 2026-09-09. The baseline is established, including the captured-artifact hash failure and environment access requirements; the gate does not assert that every pre-existing CI check is green. No application, v1 contract, source dataset or historical captured-result content changed.

Selective source mapping is recorded in [integration-sources.md](integration-sources.md). The lead checked the pinned source license/header, ACL and weighted RRF implementation, approval guard, and exact commit metadata. These are proposed ports, not implemented functionality. License copies and per-file change notices must accompany any actual adapted code.

Delegation materially contributed the frontend baseline, local service configuration/verification and provenance map. Primary verification includes Ruff, mypy, pytest, captured re-scoring, wheel content inspection, direct PostgreSQL counts and a repeated two-pass seed assertion (`PRIMARY_PG_SEED_IDEMPOTENCY=PASS`). No skipped live-model test is represented as a live integration pass.

To start the isolated local data services from the repository, run `docker compose -f deploy/compose.local.yaml up -d --wait postgres redis`. The Compose file has no variable interpolation and binds both published ports to loopback. Keep the seeded `ecommerce_p1` baseline intact; subsequent mutating integration tests use separately named disposable databases.

## Package 2 review and gate progress

Implementation is in progress across the three assigned scopes. Primary review has required corrections to SQLAlchemy column declarations, integer VND storage, product ID types, state/memory enums, claim–citation consistency, continuation bounds, turn transition locking, interrupted-turn replay, and ledger recovery/accounting. A correction is not considered verified until its relevant test passes.

The primary compared current v1 OpenAPI with the pre-package snapshot: `P2_V1_OPENAPI_STATUS=PASS`, including all five v1 paths and component schemas. CI YAML parsing and the new PostgreSQL test-service structure passed (`P2_CI_YAML_STATUS=PASS`); hosted CI has not been run. The primary reproduced the contract/authorization/registry tests: 51 passed in 35.82 seconds, with no skips or warnings. The primary also reproduced 13 migration/persistence tests in 17.86 seconds, including real PostgreSQL upgrade/downgrade/reapply, isolation, single-winner transitions, lock release and durable errors. The later provider-ledger and combined results follow below; no paid provider call occurred in Package 2.

Following the user's updated commit instruction, reviewed slices are committed as `c89dee0` (contracts/authorization/registry), `882aea0` (schema and PostgreSQL migration prerequisite), `3fcd2b2` (durable turn repository), `c01e2fa` (ledger and packaged pricing), and `c291732` (provider adapters). Each commit explains what changed, why, and how to review. These commits do not mark the package gate complete.

The primary then reproduced all 39 ledger/recovery/runtime tests in 58.92 seconds, including real PostgreSQL races and original unscoped adapter tests. Full Ruff lint/format passed (187 files); mypy passed (128 application files). Wheel build and extraction proved frontend assets and the pricing manifest are present, match source content and load from the extracted package. The unified verifier reported `VERIFY_STATUS=SUCCESS` with `VERIFY_TESTS=skipped` because pytest ran separately.

The first full Package 2 offline run finished with **392 passed, 2 failed, 2 deselected, 1 warning** in 358.54 seconds. The failures are the scripted reference's historical full-source hash compared against the modified checkout, and missing gateway request logs after programmatic Alembic migrations. The two live-provider integration tests were intentionally deselected; the existing FastAPI/Starlette TestClient deprecation warning remains. The lead assigned bounded fixes without changing historical captures or weakening log-safety assertions. Package 3 has not started.

## Package 2 gate decision

Gate met on 2026-09-09 after the final full run: **395 passed, 2 deselected, 1 warning in 326.21 seconds**. PostgreSQL checks ran against real disposable databases; no database check was skipped. The deselected tests are the two optional real-provider v1 smoke tests. No paid provider call was made, and synthetic transport checks do not establish live provider quality.

The two gate corrections were committed separately: `5bd834c` preserves existing application loggers during Alembic configuration; `504fbed` pins the immutable reference report to its verified P1 origin while retaining the existing complete 28-case current-runtime regression. The lead reproduced the ordered migration/gateway check (2 passed), reconstructed the 120-file baseline source manifest directly from Git `b7df1cc`, verified the full reference digest, and confirmed evaluation artifacts/readers remain unchanged. The final full suite passed both corrections together.

Final Ruff lint and format checks passed for 188 files; mypy passed for all 128 unchanged-since-check application files. The unified verifier again reported `VERIFY_STATUS=SUCCESS` with tests explicitly run outside the wrapper. Wheel assets and packaged pricing passed, v1 contracts remain unchanged, and the baseline database is still revision0003 with 200 products and 1,773 reviews. The pre-existing legacy captured Multi-Agent re-score immutability failure recorded under Package1 remains a separate known CI limitation; no historical report was rewritten to hide it.

The package changed 33 files after the lead reassessed the required packaging, CI and regression fixes. Sol XHigh implemented contracts, persistence and bounded gate fixes; Sol Max implemented ledger and provider-attempt concurrency/deadline behavior. Primary reviewed complete reports and diffs, validated the findings above and owns the gate decision. Package 3 may now begin.

## Package 3 kickoff

Package2 was committed at `16f0d2a` before Package3 began. The source curator (Sol XHigh) receives only 200 public bibliographic records; the ingestion/store owner (Sol XHigh) and retrieval owner (Sol Max) have separate files and must agree on typed interfaces before integration. See [package-3-corpus.md](package-3-corpus.md) for scope and gate.

The lead created a separately marked `thanh_v2_runtime` database on the existing loopback PostgreSQL service, migrated it to0005 and seeded exactly200 products/1773 reviews through the existing quality-gated importer. Its durable account `thanh-v2-provider-global` has the verified100USD limit and zero known/reserved/unknown usage at initialization. This runtime database is preserved across subsequent packages; tests continue to create their own disposable databases, and `ecommerce_p1` remains the original baseline.

The Apache2.0 license was copied byte-for-byte from source commit `94af718da1858b74b3cb4fba05ddd908ac28d9b4` to `LICENSES/Apache-2.0.txt`; SHA256 is `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`. File-level notices and exact port provenance accompany actual adaptations. No corpus has yet been published and no paid embedding has been sent.

## Package 3 reviewed slices

`5fb0994` adds strict source/mapping/index contracts and stable citation identities.
The lead reproduced four contract regressions, Ruff lint/format and mypy. The
checks cover canonical normalization, swapped work/edition IDs, malformed ACL,
non-finite vectors and tampered chunk/span identity. They do not establish the
later PostgreSQL ingestion or retrieval behavior.

`a1edfff` adds the reviewed source manifests: 20 distinct works, 200 catalog IDs,
20 `exact_work`, 17 `ambiguous` sets and 163 `unmatched` records. The lead read all
20 factual notes, independently checked their authoritative sources and validated
the complete mapping against the bounded catalog export. No initial record is
claimed as `exact_edition`. Product 58 is a milk multipack from the historical
snapshot and remains explicitly unmatched. See the committed source coverage
report for per-work limitations.

The first retrieval worker reported a Codex usage-limit error. On continuation,
the runtime confirmed that all three former worker handles were absent. Their
files were preserved, completed contract/curation work was retained, and new
workers resumed only unfinished ingestion and retrieval. The lead owns the
independent curation verification because the curator could not deliver its final
report before the interruption. No paid application-provider attempt occurred.

Review found one required schema correction: the Package 2 uniqueness rule
`(document_id, chunk_index)` cannot store two chunk layouts when a new index uses
a different chunker. A bounded migration adds explicit `chunker_version` to that
uniqueness rule. This retains the agreed PostgreSQL architecture and immutable
source/citation identities. The scope estimate increases by one migration, one
focused test file and two small existing-file changes. Package 3 remains open
until ingestion, retrieval, ACL/no-answer, actual publication and regression
checks pass together.

The lead reviewed the completed chunker migration and reproduced **21 tests in
52.56 seconds** across `test_knowledge_migrations.py`, `test_migrations.py` and
`test_v2_persistence.py`, with no skips or warnings. The tests cover SQLite and
real PostgreSQL, preserved rows/vectors/foreign keys, separate chunk layouts,
safe downgrade/reapply, and rejection of ambiguous evidence or lossy downgrade.
Backfill evidence is validated before DDL so a refused SQLite upgrade remains
retryable. Nonlegacy chunker identities must remain reconstructable on downgrade.
Ruff passed for all five changed Python files; mypy passed for the three source
files. The unified verifier reported `VERIFY_STATUS=SUCCESS`, with pytest
explicitly skipped in that wrapper because the independent run above had passed.
The preserved runtime database has not yet been migrated by this code-only slice.
