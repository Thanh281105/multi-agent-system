# Package 6 — API v2 and durable chat UI

Package 5 passed at `a329e50`. Package 6 exposes the durable runtime through
authenticated JSON and SSE transports, then connects those contracts to a
responsive chat workspace without changing the public v1 surface.

## Public transport

The JSON API covers trusted identity, mode-scoped conversations, conversation
history, non-streaming chat, turn read/cancel, action read/confirm/reject and
explicit memory CRUD. The SSE endpoint emits only the public `admitted`,
`attached`, `claimed`, `step_started`, `step_finished`, `text_delta` and
`terminal` events. Every frame has a monotonic sequence and correlation IDs.
Every connected stream reaches one terminal event. A terminal backed by a
durable turn is marked `server_settled`; a passive-timeout interruption is
explicitly unsettled so the client keeps the same recovery identity.

Snake-case wire schemas are strict. The frontend parses them into camel-case app
types at one boundary; unknown or malformed response fields fail closed. Safe
errors exclude credentials, prompts, hidden reasoning and raw tool payloads.
The reachable v1 OpenAPI projection remains byte-identical to Package 5: five
paths, eight referenced schemas and SHA256
`9530a0d564840c54c5900e234b44e21bfe6a0d40ed10165ed158e59fa7a643a0`.
The Package 5 `1B0C...` hash covered every component schema then present; it is
not the v1-only projection and therefore changes when Package 6 adds v2 schemas.

## Lifecycle and concurrency invariants

Admission locks the authorized conversation row before retry lookup and durable
turn insertion. Conversation deletion uses the same row lock, rejects an active
turn and cannot race a new admission beneath a deleted conversation. The locked
transaction contains no provider or network work.

The frontend creates one stable `(conversation_id, client_turn_id)` identity for
an intent and makes at most two SSE attachment attempts. It reconciles ambiguous
transport outcomes through the turn read endpoint. Cancel requested before the
server turn ID waits for admission, then targets that exact ID. Generation fences
discard stale callbacks after cancel, mode change, credential clearing or
conversation switch.

Action confirmation reuses one idempotency key for the same intent and checks a
readback before any bounded retry. Action rejection has no idempotency header, so
an ambiguous rejection is read back and is never submitted a second time. Both
paths preserve proposal version, expiry, required permission and terminal action
state. Memory writes require an explicit completed source turn; the client stores
selected mode/conversation and bounded pending-recovery data, including the
unsettled user message needed to reattach the same intent. Credentials,
assistant content and raw tool payloads are never persisted by the client.

## UI behavior

The v2 workspace provides:

- allowed mode and conversation selection, plus create/delete controls;
- authoritative history with at most one active turn and no duplicate rendering;
- terminal, partial, failed, cancelled and interrupted states with cancel/retry;
- citations that open the exact evidence record and version;
- product comparison, cart, order and action artifacts;
- an action dialog showing the exact before/after values, proposal/resource
  versions, expiry and required permission before confirm/reject;
- explicit memory list/create/delete with a completed source-turn selector;
- a dynamic agent roster and observed DAG edges only;
- usage and identifier ledgers with absent fields rendered as unavailable rather
  than inferred.

The v1 controller adapter remains for compatibility and regression tests; the
production application mounts the durable v2 workspace.
`DESIGN.md` tokens and the warm paper/indigo/oxide direction are preserved. The
Impeccable detector returned no findings.

## Verification evidence

Primary verification at the final package gate:

- 109 focused backend API/SSE/history/persistence/runtime/security tests passed,
  followed by 49 contract/SSE regressions after the settlement fix, against
  PostgreSQL 18.4 at `127.0.0.1:55432`;
- 116 frontend tests passed, including controller recovery/idempotency,
  presentation grounding, UI actions/memory and existing v1 accessibility;
- Oxlint and TypeScript passed; the Vite production build completed;
- Ruff lint passed and Ruff format covered 273 files; mypy passed 149 source
  files; the unified verifier reported `VERIFY_STATUS=SUCCESS`;
- the complete non-integration suite passed 750 tests with 2 optional provider
  tests deselected and the existing Starlette/httpx warning;
- exact reachable v1 OpenAPI comparison passed for 5 paths and 8 schemas;
- headless Chromium at 375, 768, 1024 and 1440 CSS pixels, device scale 200%
  and reduced motion found zero duplicate IDs, unnamed buttons, horizontal
  overflow, console errors or page errors at revision `95c9ddf`.

The browser rendering run used strict contract fixtures because the disposable
Package 6 PostgreSQL test database contains no published Package 3 corpus/index;
the normal production composition correctly returned `v2.runtime_unavailable`
instead of starting against an unpinned corpus. Real JSON/SSE, locking and
recovery behavior is covered by the PostgreSQL backend gate above.

Visual evidence:

- [375 px](screenshots/package-6-375.png)
- [768 px](screenshots/package-6-768.png)
- [1024 px](screenshots/package-6-1024.png)
- [1440 px](screenshots/package-6-1440.png)

## Reviewed commits

- `8083f57` — history and action-read contracts
- `741a9b3` — durable admission and cancellation
- `44d6ddd` — lazy production composition
- `e07de5f` — artifact and SSE public contracts
- `84db1eb` — strict frontend v2 transport
- `1495011` — durable frontend state and metadata
- `62dece3` — terminal-safe SSE recovery
- `4ffce81` — API routes and conversation lifecycle locking
- `d6c1f5c` — durable frontend controller
- `cd85cea` — grounded presentation, roster and dossier
- `3c92c83` — v2 application workspace
- `aaa27cd` — conversation-switch fence during active turns
- `bdcfd1a` — truthful durable settlement state on SSE terminals
- `95c9ddf` — same-identity frontend recovery and truthful reject readback

The complete gate and the audit remediation are recorded in the progress log.
