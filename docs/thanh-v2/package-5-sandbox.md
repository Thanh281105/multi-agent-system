# Package 5 — durable history, explicit memory and sandbox actions

Package 5 begins from the committed Package 4 runtime at `93c7cf7`. It keeps
PostgreSQL authoritative, keeps v1 unchanged, and implements the existing v2
schema and contracts through narrowly separated services.

## Scope and ownership

The durable history service restores owner-authorized transcript data after a
new process/session, loads bounded current context, stores only explicit
source-bound book/language/budget preferences, and deletes a conversation in
one transaction with pending-proposal invalidation and deletion of memories
sourced from that conversation.

The sandbox seed copies positive historical snapshot prices into separate demo
offers with deterministic IDs and stock 10. It inserts missing offers only;
reruns never overwrite price, stock, version, activity or other merchant/user
changes. A partial unique active-cart constraint and immutable audit guard close
the remaining database races without touching products, reviews or v1.

The action service owns cart state, checkout and merchant offer changes. A
proposal ID is also its stable public action ID. Confirmation cards carry the
proposal version, exact target, before/after values, data versions and a
database-time expiry ten minutes after preview. Model prose cannot approve an
action, and no model/network call occurs inside an action transaction.

## Transaction invariants

Authorization and strict contract validation happen before each transaction;
all resource identity remains server-derived. The transaction then locks the
live owner-scoped conversation/turn/proposal, idempotency record, cart, sorted
lines and sorted offers as applicable. It rechecks proposal and target
versions, expiry, price, stock, activity and the canonical before-state before
performing an effect and appending audit.

The same idempotency key with the same canonical confirmation payload returns
the identical stored result. Reusing the key with a different payload is a
conflict. Rollback before commit leaves no effect or durable idempotency result;
a lost response after commit is read/replayed and never dispatches the action
again. Price, stock, offer or cart change after preview makes the proposal
terminally stale and requires a new preview.

Checkout creates one immutable order and item snapshot, decrements each offer
once, marks the active cart checked out and consumes its pre-update version
once. Merchant price/stock confirmation changes only demo offers and increments
the offer version once. Confirmation/rejection and concurrent confirmations
serialize on the proposal; the first committed terminal decision wins. Chat
cancellation never reverses a committed action.

## Gate

Primary review and tests must cover:

- restart-safe history and current owner authorization;
- bounded context where current request constraints override stored memory;
- explicit preference source binding and deletion cleanup;
- deterministic insert-only seeding, rerun preservation and concurrent seed;
- shopper demo-price/cart/checkout and merchant inventory/proposal behavior;
- proposal expiry and stale price, stock, offer and cart versions;
- same-key replay, different-payload conflict and lost-response recovery;
- concurrent confirm/confirm and confirm/reject with one durable effect;
- immutable order/items, one stock decrement/cart consumption and matching audit;
- cross-tenant, principal, mode and store isolation;
- full v1/OpenAPI regression plus lint, format, mypy and full relevant pytest.

Only a passing real-PostgreSQL restart/retry/confirmation/concurrency gate permits
Package 6 API and frontend work.

## Reviewed slices

The reviewed Package 5 slices are committed in order: `5a4a942` opens the
package gate; `6fb8cf9` adds durable history, bounded context and explicit
source-bound memory; `a558aef` adds deterministic demo-offer seeding and the
active-cart/audit guards; `76a7443` adds transactional cart, checkout and
merchant-offer actions; `ed39233` hardens proposal identity and action races;
`6a49233` connects sandbox reads to the planner and tools; `094657c` restores
durable conversation context after a new claim; `afa7961` adds stale, replay,
restart, deletion and immutable-order regressions; `aa3dfcf` adds guarded chat
proposal creation with server-owned cards; and `5f42cf0` closes proposal
recovery, database-clock lease fencing and history timestamp ties.

The P1 mixed-clock audit found that application `datetime.now(UTC)` checks could
be compared with lease timestamps governed by PostgreSQL, allowing application
clock skew to accept or reject a lease incorrectly. The final slice makes
`clock_timestamp()` the database authority for claim, active-worker fencing and
expiry/recovery checks, with the lease check held under the locked turn row. It
also recovers an expired proposal turn by replaying its stored card without
rerunning planning, tools or model work. A history timestamp-tie regression was
fixed at the same time: when turns share `created_at`, context ordering now uses
`completed_at` and then the stable turn ID.

The original 8–12-file estimate is reassessed against the actual slices. The
package currently spans 19 tracked application, test, migration and seed-script
files, plus these two documentation files. The increase is explained by the
separate history, sandbox seed/read, action,
planner/runtime recovery and real-PostgreSQL regression boundaries. It does not
change the agreed v2 service or PostgreSQL architecture.

## Verification evidence

The focused final set passed **171 tests in 149.91 seconds**. The final
action/runtime set passed **60 tests in 52.25 seconds**; the supervisor plus
continuation set passed **84 tests in 114.33 seconds**. PostgreSQL verification
used a native disposable PostgreSQL **18.4** server at `127.0.0.1:55432`, with
no PostgreSQL skips. Docker Desktop Engine was unavailable for this run, so
these results do not claim a Docker test.

Ruff lint passed for 229 files, Ruff format passed for 229 files, and mypy
passed for 143 application files. The v1 OpenAPI snapshot exactly matches the
Package 4 commit `93c7cf7`: both SHA256 values are
`1B0C0F4AAD5613C9373FAD826030DCF4C770E08063CCA48C020CD8432AB96C9B`, covering
5 v1 paths and 8 schemas.

## Current gate status

Package 5 gate **passed on 2026-09-12**. The final CI-equivalent
`pytest -m "not integration" -q` run completed with **703 passed, 2 deselected
and 1 warning in 727.75 seconds (12:07)**. The deselected cases are the two
optional integration-marked provider tests. The warning is the existing
Starlette `TestClient` deprecation warning for the installed `httpx` transport.
Package 6 may now begin.
