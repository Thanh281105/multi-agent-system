# thanh-v2 execution progress

Implementation contract: [approved plan](implementation-contract.md). Work packages run sequentially; the next package starts only after the current gate is met. Following the user's updated instruction, independently reviewed and tested slices are committed as they finish, using [commit.md](../../commit.md), with a final gate record for each package. Package 1 records baseline failures as required by its gate; subsequent gates require their named tests to pass.

| Package | State | Gate / evidence |
| --- | --- | --- |
| 1 — branch, environment, baseline, provenance | Committed `6b046d4` | Baseline and environment status established; tests, local database and attribution reviewed. Known captured-artifact hash failure recorded below. |
| 2 — contracts, registry, schema, authorization, budget | Committed `16f0d2a` | 395 tests pass; contracts, real PostgreSQL, exact-cost ledger and v1 regression verified. Seven implementation/fix commits plus the gate record. |
| 3 — corpus, mapping, retrieval, citations | Committed `7c9d86d` | 462 tests pass; published 20-work/200-mapping corpus, real embeddings/ledger and development no-answer calibration verified. |
| 4 — supervisor, continuation, deduplication, grounding | Committed `93c7cf7` | 588 tests pass; real PostgreSQL, live grounded read/replay, usage ledger and exact v1 OpenAPI verified. |
| 5 — history, memory, sandbox actions | Gate passed | 703 tests pass; real PostgreSQL recovery/concurrency, clock skew and exact v1 OpenAPI verified. |
| 6 — API and frontend | Gate passed | JSON/SSE/UI consistency; v1 regression; PostgreSQL-backed v2 boundary |
| 7 — evaluation v3, gold, pilot, freeze | Successor pilot pending | Historical corrected pilot is immutable but source remediation changed the canonical protocol; new P7 dry-run is valid and awaits live dispatch/freeze |
| 8 — benchmark, error analysis, final checks/docs | Historical terminal partial / final gate open | Historical P8 has 678/720 SUT cells and remains immutable; successor P8 starts only after successor P7 is complete and frozen |

## Execution rules

- Lead owns review, integration, gates and commits. Following the current routing instruction, Luna Max implements bounded tasks; Sol High is reserved for architecture, hard debugging, transactional concurrency/idempotency, supervisor and grounding.
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
The slice was committed as `2424a68`. The lead then applied it only to the marked
runtime database and confirmed schema/model agreement, unchanged 200 products and
1,773 reviews, zero known/reserved/unknown account usage, and an untouched revision
0003 baseline (`P3_RUNTIME_CHUNKER_MIGRATION=PASS`).

Eight development relevance probes are now fixed in
`data/knowledge/books-v1/development-probes.json`. Primary validation confirmed
all queries and expected sources match the pre-SUT plan, all four positive work
groups are excluded from held-out evaluation, and no probe text entered the
corpus (`P3_PROBES_GOLD_PREFLIGHT=PASS`). Threshold calibration and real provider
collection have not yet run. The new calibration runner is a bounded Package 3
verification task assigned to Sol XHigh; it does not begin Package 7.

The lead independently reviewed the frozen retrieval/service slice and reproduced
**28 tests in 22.62 seconds**, without skips or warnings. Ruff lint/format passed
for its four files and mypy passed for both modules. Tests cover ACL-before-plan
ordering, source allowlists, exact citation reopening, hybrid/required failure
semantics, cancellation, raw relevance abstention, weighted rank provenance and
the exact rendered Unicode context bound. These are fixture regressions; the
combined PostgreSQL and real-provider checks remain separate open gate items.
The lead also verified the pinned upstream blob IDs and actual algorithm lineage,
and recorded the adaptations in `integration-sources.md`.

The lead reproduced **31 tests in 33.00 seconds** across ingestion, the six real
PostgreSQL scenarios and the development-calibration fixtures, with no skips or
warnings. The PostgreSQL facade test proves tenant/principal/mode/scope filtering
before planning and diagnostics, no provider/planner work for an empty authorized
catalog, exact citation reopening, and refusal after principal/scope changes or
version/span tampering. Other scenarios cover vector reuse, stable batch resume,
concurrent dispatch fencing and separate model/dimension/chunker/enrichment
fingerprints while preserving the old index.

All seven ingestion/calibration source and test files passed Ruff lint/format;
the four source modules passed mypy. Their frozen SHA256 values matched the
workers' reports. Primary's unified verifier used the repository venv and
`PYTHONPATH`, reported `VERIFY_STATUS=SUCCESS`, and explicitly skipped its pytest
step because the independent run above passed. The worker's earlier bare-pytest
path contamination is not treated as a passing test result. Live provider
publication, threshold calibration and the full v1 regression gate remain open.

Ingestion/PostgreSQL is committed as `02ff49a`; the calibration runner and its
regressions are committed as `fcce567`. Primary then completed actual publication
and development calibration through the preserved common ledger. The final
20-source/20-vector/200-mapping corpus uses policy `p3_cal_d025_l010`; publishing
that policy reused every vector and cost zero additional attempts. The direct
published-store audit passed all eight fixed probes and reopened 24 evidence
references exactly. Positive probes contain extra sources, so this is relevance
and abstention evidence only, not semantic grounding or benchmark quality.

Two real embedding attempts used 2,632 input tokens and 52,640 nano-USD
(0.00005264 USD). Pending/reserved/unknown/missing usage are all zero. Runtime and
baseline catalog counts remain 200/1,773, at revisions 0006 and 0003 respectively.
The full app passed Ruff lint/format (202 files), mypy (133 modules) and exact
comparison of all five v1 paths/component schemas against the earlier snapshot.
The complete regression result and gate decision follow below.

## Package 3 gate decision

Gate met on 2026-09-09 after the primary full run: **462 passed, 2 deselected,
1 warning in 508.23 seconds**. No PostgreSQL scenario was skipped. The deselected
tests are the two optional v1 live-provider smoke tests, and the warning is the
pre-existing Starlette/httpx deprecation. The separate real-provider corpus and
calibration calls above establish the live embedding boundary.

The lead reviewed all worker reports and frozen files, reproduced the focused
and combined tests, verified source attribution, and inspected the generated
calibration/runtime artifacts. SQL ACL-before-plan, stable evidence IDs and spans,
fingerprint changes, restart-safe batch reuse and concurrent fencing passed. The
full suite covers the shared ledger and v1 regression; exact v1 OpenAPI and all
existing evaluation artifacts remain unchanged. Frontend code did not change in
this package; its earlier verified baseline remains applicable.

The package contains 17 source/test/migration files, within the reassessed
12–18-file estimate, plus the required curated inputs, license, documentation and
five generated calibration/publication artifacts. The only schema adjustment was
the documented chunker uniqueness migration. No semantic-grounding or benchmark
quality claim is inferred from the development probes. See
[package-3-corpus.md](package-3-corpus.md) for exact runtime IDs, recorded limits and
costs. Package 4 may now begin.

## Package 4 kickoff

Preflight after `7c9d86d` confirms clean `thanh-v2` and the same CodeGraph/sensitive
file boundaries. Sol Max receives supervisor/continuation and semantic grounding.
The intended Sol XHigh read-tool dispatch hit the runtime's agent-thread limit;
the lead owns that bounded integration while both workers proceed. The lead also owns interface review, durable
integration, live credentials, regression gates and small commits. See
[package-4-runtime.md](package-4-runtime.md) for scope, sequencing and acceptance.

## Package 4 reviewed slices

`75c0c51` fixes the shared PostgreSQL author-search expression. The first real
read-tool run found that direct JSON-to-text matching missed escaped Vietnamese
names while SQLite passed. PostgreSQL now decodes JSON through JSONB before text
matching; SQLite behavior and all v1 contracts remain unchanged. The lead ran
**27 search/domain-agent/PostgreSQL regressions**, plus **22 read-tool integration
tests**, with no skips or warnings. Ruff and mypy passed; the unified verifier
reported `VERIFY_STATUS=SUCCESS` with pytest explicitly run separately.

The lead's ongoing interface review required exact Decimal persistence, current
ACL validation before model context, rejection of misleading partial quotations,
and answer coverage for explicit evidence obligations. Supervisor review also
requires genuine model planning, staged initial dependencies that do not consume
the continuation allowance, and separate expert reasoning over shared tools.
The reviewed internal contract, read tools, grounding verifier and answer producer
are now committed separately as `0dfb5e4`, `213374a`, `399970f` and `c9302ae`.
Primary reproduced 57 combined grounding/read-tool/PostgreSQL tests, then the new
source-revocation-before-repair regression and two adjacent repair cases after the
final answer-layer correction. Ruff eight files and mypy four modules passed.
Source blobs, Apache headers and the exact frozen worker hashes were reviewed.

`99c2883` adds the small internal runtime-metadata persistence seam. Primary ran
14 new metadata tests and 21 existing persistence/migration regressions, then
reran the two final migration fixtures. The unified verifier reports
`VERIFY_STATUS=SUCCESS` with pytest run separately. Runtime alone was upgraded to
0007; before/after hashes of the baseline, catalog, corpus, vector and budget
tables match. The baseline remains at 0003. No generation call has been made by
Package 4 at this checkpoint.

A bounded Sol Max concurrency review found a check/write lease race and two
exception-cleanup cases. That worker now owns the atomic repository fence and its
PostgreSQL regressions; the supervisor owner wires the guarded writes and finishes
the genuine model-template selection, fractional-price and replay-usage fixes.
The lead reproduced the atomic fence with six PostgreSQL tests and committed it as
`7103085`. Planner, durable execution and supervisor were then committed separately
as `8513a3f`, `eb59550` and `f1a2eb2`. After the user's routing update, final
in-workspace inspection used Sol High; no completed work was restarted.

## Package 4 gate decision

Gate met on 2026-09-10. Primary verification passed **126 Package 4 integration
tests in 178.35 seconds**, including real PostgreSQL races, then **588 full tests,
2 deselected and 1 warning in 863.89 seconds**. The deselected cases are the two
optional v1 live-provider tests; the warning remains the known Starlette/httpx
deprecation. No PostgreSQL test was skipped. Ruff lint/format passed for 242 files,
mypy passed for 140 source files, and exact v1 OpenAPI comparison retained all five
paths and component schemas. Package 4 changed no historical evaluation source or
captured result.

The live development catalog read in `off` mode completed and replayed with zero
generation calls, provider attempts or cost. The `required` knowledge read used the
previously excluded Sapiens development group and the frozen corpus/index. It
completed with one retrieval, six generation calls, seven total provider attempts
(including embedding), one bounded repair, 6,903 total tokens and 0.00897707 USD
known cost. The model repair was not grounded, so the answer layer truthfully marked
fallback and emitted only checked catalog facts plus the exact authorized source
extract. Cross-process retry returned the stored result and identical usage without
another dispatch.

The preserved global account moved from 52,640 to 9,029,710 nano-USD known usage;
reserved and unknown usage remain zero. Baseline/runtime databases remain at
revisions 0003/0007 with 200 products and 1,773 reviews each. This development run
proves the bounded execution, grounding and accounting path; it is not a benchmark
quality claim. See [package-4-runtime.md](package-4-runtime.md) for the detailed
limits and evidence. Package 5 may now begin.

## Package 5 kickoff

Preflight after `93c7cf7` confirms a clean `thanh-v2` worktree and healthy
Package 4 boundary. The Package 2 schema already supplies owner-scoped
conversation, preference, offer, cart, immutable order, proposal, idempotency
and audit records; Package 5 adds their durable service behavior instead of
replacing the schema or v1 runtime.

Three bounded read-only investigations mapped the work before file ownership
was assigned. Luna Max owns durable history/memory and the insert-only sandbox
seed/guard slices. Sol High owns action transactions, stable lock ordering,
idempotent replay, stale previews and concurrency tests. The lead rejected a
redundant proposal action-ID column: the stable proposal ID is also the public
action ID. Existing `genre` and `author` preferences remain valid book
preferences alongside language and budget; memory still requires an explicit,
source-bound request and never infers sensitive traits.

Expected change budget is 8–12 files across history/context, sandbox seed/read
state and action execution. Real PostgreSQL must prove restart, current ACL,
conversation deletion, concurrent seed, confirmation/rejection races,
same-key replay, different-payload conflict, stale price/stock/cart previews,
single immutable order, one stock decrement and one consumed cart version.
See [package-5-sandbox.md](package-5-sandbox.md) for the scoped gate.

## Package 5 reviewed slices and verification

The reviewed Package 5 slices are committed as `5a4a942`, `6fb8cf9`, `a558aef`,
`76a7443`, `ed39233`, `6a49233`, `094657c`, `afa7961`, `aa3dfcf` and `5f42cf0`.
Together they add durable history/memory, deterministic insert-only demo offers,
transactional cart/checkout/merchant actions, sandbox reads, guarded chat
proposals, recovery, database-clock lease fences, stable history ordering and
their PostgreSQL regressions.

The P1 mixed-clock audit found application-clock lease checks beside
PostgreSQL-controlled lease timestamps. The final fix uses the database
`clock_timestamp()` for claim, active-worker fencing and expiry/recovery, and
checks the lease under the locked turn row. Expired proposal turns recover by
replaying their stored card without new planning, tool or model work. The same
slice fixes history ordering when turns tie on `created_at` by ordering next on
`completed_at`, then the stable turn ID.

The original 8–12-file estimate is reassessed at 19 tracked application,
test, migration and seed-script files across the actual slices, plus the two
Package 5 documentation files. The extra coverage belongs to separate history,
sandbox seed/read, action, planner/runtime recovery and real-PostgreSQL
regression boundaries; the v2 service and PostgreSQL architecture is unchanged.

The focused final set passed **171 tests in 149.91 seconds**. The final
action/runtime set passed **60 tests in 52.25 seconds**, and the supervisor plus
continuation set passed **84 tests in 114.33 seconds**. PostgreSQL verification
used a native disposable PostgreSQL **18.4** server at `127.0.0.1:55432`, with
no skips. Docker Desktop Engine was unavailable, so this record makes no Docker
test claim.

Ruff lint passed for 229 files, Ruff format passed for 229 files, and mypy
passed for 143 application files. The v1 OpenAPI snapshot exactly matches
Package 4 commit `93c7cf7`; both SHA256 values are
`1B0C0F4AAD5613C9373FAD826030DCF4C770E08063CCA48C020CD8432AB96C9B`, covering
5 v1 paths and 8 schemas.

Package 5 gate passed on 2026-09-12. The final CI-equivalent
`pytest -m "not integration" -q` run completed with **703 passed, 2 deselected
and 1 warning in 727.75 seconds (12:07)**. The two deselected cases are the
optional integration-marked provider tests. The warning is the existing
Starlette `TestClient` deprecation warning for the installed `httpx` transport.
All required PostgreSQL cases ran without skips, and Package 6 may now begin.

## Package 6 kickoff

Package 6 started from the passed Package 5 gate at `a329e50`. The lead split
the public API, durable turn lifecycle, frontend transport/state, presentation
and application wiring into exclusive backend/frontend slices. UI work followed
the UI UX Pro Max, Impeccable and shadcn project pipeline and preserved the
warm paper/indigo/oxide constitution in `DESIGN.md`.

The package exposes authenticated v2 identity, conversation/history, chat,
turn read/cancel, action decision/read and explicit memory endpoints. JSON and
SSE share strict snake-case contracts; the frontend converts them once into
camel-case state. Conversation admission and deletion serialize on the same
authorized row, and retries retain a stable conversation/client-turn identity.
The final scope is 42 tracked source/test files across the planned API and UI
areas. No migration, dependency or architectural deviation was needed.

## Package 6 reviewed slices and remediation

The reviewed implementation commits are `8083f57`, `741a9b3`, `44d6ddd`,
`e07de5f`, `84db1eb`, `1495011`, `62dece3`, `4ffce81`, `d6c1f5c`, `cd85cea`,
`3c92c83` and `aaa27cd`. They add the public contracts, durable admission and
cancellation, lazy production composition, JSON/SSE routes, strict frontend
transport/state, recovery controller, grounded presentation and responsive v2
workspace.

A final read-only Sol XHigh gate audit found that passive SSE timeout emitted a
synthetic interrupted terminal while the durable turn remained live. The client
previously treated that event as settled, cleared recovery metadata and could no
longer reattach correctly. Sol Max implemented the backend settlement contract;
Sol XHigh implemented the frontend recovery/action corrections. The lead reviewed
and reproduced both. Commit `bdcfd1a` requires `server_settled` on every terminal,
marks passive timeout false without cancelling or overwriting the live lease, and
marks durable outcomes true. Commit `95c9ddf` keeps the same recovery tuple,
resets only the per-connection event sequence, blocks a new intent until durable
settlement, and reports action rejection success only after a `rejected` readback.
Direct UI tests cover cancelled, interrupted, expired and conflicted states.

The audit also corrected documentation: session storage contains selected mode,
conversation and bounded pending-recovery data including the unsettled user
message, but no credential, assistant content or raw tool payload. The production
application mounts v2; the v1 controller adapter remains a compatibility/test
boundary. The reachable v1 OpenAPI projection is byte-identical to Package 5:
5 paths, 8 referenced schemas and SHA256
`9530a0d564840c54c5900e234b44e21bfe6a0d40ed10165ed158e59fa7a643a0`.
The older `1B0C...` value covered all component schemas then present rather than
the v1-only reachable projection.

## Package 6 gate decision

Package 6 passed on 2026-09-14. Primary verification ran 49 contract/SSE tests
against PostgreSQL 18.4 after the settlement fix, then the complete
`pytest -m "not integration" -q` gate: **750 passed, 2 deselected and 1 warning
in 935.00 seconds**. No PostgreSQL test was skipped. The deselected tests are the
two optional live-provider integration cases; the warning is the existing
Starlette/httpx deprecation.

Frontend verification passed **116 tests**, Oxlint and the TypeScript/Vite
production build. Ruff lint passed and Ruff format checked 273 files; mypy passed
149 source files. The unified verifier reported `VERIFY_STATUS=SUCCESS` with
language-specific tests recorded separately. The exact v1 comparison passed.
Chromium QA at 375, 768, 1024 and 1440 CSS pixels, 200% device scale and reduced
motion passed at `95c9ddf`: no duplicate IDs, unnamed buttons, horizontal
overflow, console errors or page errors. Strict fixtures were used only for the
visual browser run because the disposable Package 6 database has no published
Package 3 corpus/index; real API, locking and recovery behavior ran against
PostgreSQL in the backend gate. Package 6 is therefore closed; the P7 freeze and
pilot handoff follows below.

## Package 7 historical evidence and successor handoff

The P7 evaluation inputs are frozen in committed `evaluation/v3/`; operator-local
artifacts are not committed. Historical corrected pilot artifacts are under
`output/evaluation-v3/pilot-corrected-v3/`. Its protocol SHA256 is
`385ae09e74a7e8e2896b170f9ad3f2549f6f79390e94b3241cd7da0e4b32721f`. It binds
four variants: `sa_shared_tools_rag`, `ma_fixed_rag`, `ma_adaptive_rag` and
`ma_adaptive_no_rag`. The frozen split contains 20 development cases and 60
held-out cases.

The corrected P7 pilot completed `36/36` terminal cells. The global repeat
decision selects three repeats for the held-out matrix. Actual pilot effective
cost is `0.05332290 USD`; projected held-out SUT cost is `5.72929200 USD`. The
semantic judge is the declared automated `model_judge`
contract; it is not a human judge. Gold authorship is `automated_pre_sut_spec`,
with no human author or judge IDs.

These artifacts establish historical frozen inputs and a budget projection only.
They do not establish held-out quality, semantic grounding, or a completed
benchmark. Merchant remediation in `ea24338` changes the source-bound protocol;
the canonical successor SHA256 is
`c8a4b4fb912039b93071fa37030cdbb11971cf3a15c65000d7c7e2ddfff79cde`.
`run_p7_successor_v4` has passed local dry-run with 36 cells (4 warmup and 32
measurements), schedule SHA
`f37eaa4482cafa85b9c665bf6ea25f30a81872df277e3e803949035209b7da13`, but has
not dispatched a provider run and has no repeat decision. It must complete and
freeze before a new P8 schedule may be created. No completed historical cell may
be replayed or reused as successor evidence.

The merchant remediation keeps ambiguous multi-product price mutations safe: it
returns a clarification instead of choosing a target or fabricating a proposal.
The successor evaluation must record that observable behavior against its gold
contract; it must not alter the protocol or force a proposal merely to reproduce
a previous expected state.
The published v2 runtime remains PostgreSQL-backed and pins corpus
`books-v1-calibrated-20260909` with 20 sources/chunks/vectors, 200 mappings
(20 exact work, 17 ambiguous, 163 unmatched), and
`text-embedding-3-small` at 1,536 dimensions. Benchmark Q/A and calibration
artifacts remain outside that corpus.

## Package 8 current state

Package 8 is additive. `python -m app.evaluation.benchmark_cli` provides local
`validate`, `dry-run`, `prepare`; guarded SUT `run`/`resume`; and guarded
post-SUT `operate`. `operate` rebuilds exact-evidence preparation under the
receipt authorization, calibrates on the frozen successor P7 pilot measurements,
freezes the judge, runs the blind held-out journal and publishes final artifacts.
It requires explicit `--allow-network`, `--database-url` and a per-job judge
budget; it fails closed when a catalog/review citation lacks an immutable exact
authority. The driver builds the exact `60 × 4 × 3 = 720` held-out measurement
schedule and uses append-only checkpoint/resume behavior. Local commands do not
call a provider.

The held-out SUT run `run_p8_heldout_corrected_v3` reached a terminal partial
checkpoint: 678 of 720 scheduled cells completed and 42 failed, with 0 pending,
0 ambiguous and 0 orphan cells. Its schedule SHA is
`a6002ab2dc15282bf4b26c79ffece061242065e8352e637a1a82623a8fa1fd71`. The
immutable `partial-execution-report.p8.json` records 792 generation/provider
attempts, 0 retries, 0 embeddings, `1.44856770 USD` known SUT cost and no
unresolved reservation. Safe terminal error counts are 1
`expert_selection_not_authorized`, 2 `model_response_incomplete`, 4
`model_response_invalid` and 35 `turn_execution_failed`; no raw provider
payload is carried into the report. Partial report checksum is
`17177bd7829d972c159d77ca068852fafd10d12152f2d087db07172f768f62ea`, and the
execution-case-set SHA is
`26cb17e6dbc549f871b69ef682b21af9f32fa69a05d8c671e297b269c0040339`.

`partial-report` reads that checkpoint with a forbidden live executor and writes
or validates the immutable partial record. It does not dispatch a provider,
score receipts, reopen citation authority, calibrate the judge, or publish a
benchmark result. The append-only checkpoint cannot redispatch settled cells, so
this run cannot become a complete result through `resume`. P8 remains open for a
distinct successor P7/P8 protocol and run. The successor P8 may only be created
after `run_p7_successor_v4` completes and its repeat decision is frozen; it then
requires exact evidence resolution, calibration/judging and the final gates.

Package 8 remains open until all of the following are complete and independently
validated: calibrated automated judge and frozen bindings; exact immutable
evidence resolver over source/version/chunk/span; all 720 receipt cells with
valid ledger attribution and no missing/ambiguous work; blind-answer/judgment
join; analysis/report artifacts; real PostgreSQL transactional/replay checks;
frontend checks when affected; package gates; and final documentation review.
