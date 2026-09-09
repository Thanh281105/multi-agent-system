# Package 2 — foundation boundaries

Package 1 gate met and committed at `6b046d4`. This package implements foundation contracts/services and tests; public v2 endpoints and UI wiring remain Package 6, with read orchestration Package 4 and complete business services Package 5.

Three independent implementation scopes: v2 contracts/registry/authorization (Sol XHigh), durable models/migrations/repository (Sol XHigh), shared provider attempt ledger/runtime integration (Sol Max for reservation concurrency and ambiguous provider outcomes). Expected budget: approximately 20–25 files across these three foundations. Any larger architectural change requires lead reassessment.

Integration review adds three necessary existing-file updates: the schema-head references in architecture/data documentation and a disposable PostgreSQL service in CI. The database tests require real PostgreSQL and fail clearly when it is unavailable; CI must provide the same prerequisite as the local gate. Existing CI checks are preserved.

The pricing manifest is a runtime asset at `app/shared/pricing-manifest.json`. Package metadata includes it in the wheel so the same pinned manifest is available in editable installs, standalone wheels and the existing Docker build. This requires one focused `pyproject.toml` package-data update; no dependency is added.

Review added one separate PostgreSQL ledger-race test module so its author can work independently while the budget owner fixes provider/runtime behavior. This expands the file estimate by one without adding a new subsystem.

The full regression gate exposed two additional corrections: preserve existing application loggers when Alembic configures logging (one source line and a regression test), and validate the immutable historical reference against its verified original source instead of the current checkout (test-only). The lead reassessed these changes before assignment. Final package scope is 33 files, including the original foundations, their tests, runtime packaging/CI prerequisites and documentation. No new architecture, dependency or service beyond the approved plan was introduced.

- Reuse existing `AuthorizationContext` and `TaskStatus`; do not add fields or enum values to v1 contracts. A separate v2 turn lifecycle distinguishes cancelled/interrupted from valid dialogue outcomes.
- Trusted mode policy: shopper requires `ecommerce.read`; merchant requires `merchant.read`. Shopper writes require `ecommerce.write`; merchant writes require `merchant.write`. Merchant mode also requires `ecommerce.read` for shared catalog tooling. Ownership checks bind tenant, principal, mode and fixed store, and re-check current scopes. No identity, tenant or role is accepted from model output. Store assignment is server-owned (`demo` by default), not an arbitrary client-supplied foreign store.
- New table names are prefixed `v2_`; globally unique string IDs, UTC timestamps, integer VND and integer/numeric exact USD accounting. JSON columns may use PostgreSQL JSONB variants. SQLite is for regression only, never transaction proof.
- Persistence migration 0004 covers conversations/turns/step results/preferences, sandbox offers/carts/orders/proposals/action idempotency/audit, versioned knowledge documents/chunks/mappings/corpus/index manifests. No demo seed in migrations. Budget owns its models and migration0005. ORM metadata imports both, with final expected revision0005.
- Persist each chat turn before provider dispatch, unique on conversation/client_turn_id with payload hash conflict detection. Store final result before terminal output; completed step uniqueness supports later continuation. Repository access always receives current trusted authorization.
- The budget subsystem must be opt-in for v2 via a scoped context in existing provider adapters, preserving unscoped v1 behavior. One provider loop per adapter. Every real attempt reserves before transport and settles known usage before parsing; unknown usage remains reserved. Offline fake transports test these boundaries.
- Session/tenant isolation, model/pricing allowlists, combined retry/embedding attempt limits and concurrent reservations are package gates. No paid requests before ledger verification.

The existing configured OpenAI key was checked for presence only by the lead. User authorized the existing runtime and budget in the implementation contract; no new key or credential mutation is needed. Secrets are excluded from all delegated contexts.

## Shared interface decisions

Contracts and repository share immutable `ResourceBinding` and `ResourceAuthorization`; `require_mode_access`, `bind_request_authorization` and `authorize_resource_access` re-check the current trusted scope on each operation. The request binder selects `DEMO_STORE_ID=demo`; cross-owner resources return uniform `ResourceNotFoundError`. Client contracts omit tenant/role/store and reject extra fields.

Provider pricing was checked against official OpenAI model pages on 2026-09-09: [GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini), [GPT-5.4 nano](https://developers.openai.com/api/docs/models/gpt-5.4-nano), and [text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small). The generation snapshots shown are `gpt-5.4-mini-2026-03-17` and `gpt-5.4-nano-2026-03-17`. The checked standard text prices per million tokens are mini input/cached/output USD0.75/0.075/4.50, nano USD0.20/0.02/1.25, embedding USD0.02 input. The manifest scope must match the actual service tier and endpoint; these values do not authorize a model upgrade or establish account availability.

## Integration invariants for later packages

V2 usage and cost summaries must come from the durable attempt ledger, including unknown reservations and retries. Existing v1 `ModelCallMetadata` remains a compatibility trace and must not become the v2 cost authority. Corpus ingestion, query embeddings, warmup, pilot, benchmark and judge must all enter an explicit ledger scope before using the common adapters.

Scoped SDK clients disable internal retries so each adapter attempt corresponds to one ledger reservation. Client creation is lazy to preserve unscoped v1 constructor behavior. A recovered attempt may reconcile later authoritative usage into exact cost once while retaining its terminal execution status; reconciliation must not decrement the active-attempt count twice.

Both scoped transports must enforce the total attempt deadline. [HTTPX timeout settings](https://www.python-httpx.org/advanced/timeouts/) bound separate connect/read/write/pool operations, so the synchronous SDK timeout alone does not prove a total 18-second cap. The scoped embedding loop uses an async transport with an outer deadline inside the existing adapter; the public synchronous interface remains intended for worker threads or CLI callers. No dependency or recovery service is added for this enforcement.

The original seeded baseline remains separate from disposable PostgreSQL gate databases. Public v2 endpoints are wired in Package6 after read orchestration and durable sandbox services have passed their own gates; foundation tests must still prove strict contracts and actual database behavior now.

## PostgreSQL test prerequisite

Start `deploy/compose.local.yaml` as described in [progress.md](progress.md), then set the test-server URL before running the suite:

```powershell
$env:TEST_POSTGRES_URL = 'postgresql+psycopg://ecommerce_p1:p1-local-disposable-only@127.0.0.1:55432/ecommerce_p1'
.\.venv\Scripts\python.exe -m pytest -m 'not integration' -q
```

These credentials belong only to the disposable local Compose service. The test helper connects to the maintenance database, creates a uniquely named `thanh_v2_p2_` database per fixture and cleans up only that database. It does not migrate, seed or clear the baseline database named in the URL. Missing test-server configuration is a gate failure, not a skipped PostgreSQL check. CI supplies a dedicated PostgreSQL service and URL for the same suite.
