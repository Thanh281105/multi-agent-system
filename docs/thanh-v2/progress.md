# thanh-v2 execution progress

Implementation contract: [approved plan](implementation-contract.md). Work packages run sequentially; a package is committed only after its gate has been assessed and met. Package 1 records baseline failures as required by its gate; subsequent gates require their named tests to pass.

| Package | State | Gate / evidence |
| --- | --- | --- |
| 1 — branch, environment, baseline, provenance | Gate met | Baseline and environment status established; tests, local database and attribution reviewed. Known captured-artifact hash failure recorded below. |
| 2 — contracts, registry, schema, authorization, budget | Not started | Contract/migration/auth/budget tests |
| 3 — corpus, mapping, retrieval, citations | Not started | Valid corpus version; retrieval/ACL/no-answer |
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
