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
