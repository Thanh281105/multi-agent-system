# Package 4 — bounded supervisor and grounded read turns

Package 3 passed at `7c9d86d`. Preflight reports clean `thanh-v2`, CodeGraph
available and no repository-local AGENTS files. The user-provided instructions
remain applicable. The existing v2 contracts, registry, authorization, repository,
ledger and published knowledge corpus are the integration boundary.

## Scope and execution order

The expected change is 19–23 Python source/test files plus this document,
progress and adaptation provenance. The lead delegates two disjoint scopes:
supervisor/planning/execution (Sol Max) and grounding/answer generation (Sol Max).
The intended read-tool worker (Sol XHigh) could not start because of the runtime's
agent-thread limit, including an attempted reuse of the previous XHigh worker.
The lead therefore owns the bounded read-tool integration. Shared internal types are proposed first
and reviewed before cross-component wiring. No new framework, dependency or
provider loop is required.

The estimate increased by two files after real PostgreSQL testing exposed an
existing Unicode author-search defect: casting JSON directly to text preserves
escaped characters. A small shared query correction decodes JSON through JSONB
on PostgreSQL before text matching, with a separate regression test for search
and statistics. It preserves SQLite behavior, v1 schemas and historical rows.

A further five-file persistence correction is necessary because failed/cancelled
turns correctly have no result payload, while the provider ledger does not store
draft-repair stage labels. A small internal `runtime_metadata` column binds the
three data versions before claiming and checkpoints attempted retrieval/repair
counts under the current lease before dispatch. It preserves these facts across
restart without changing public error/result shapes or client retry identity.
Bindings are immutable, counters monotonic and capped, and a downgrade refuses to
discard nonempty metadata. This is a completion of the durable-turn contract.

An independent concurrency review also requires active-worker step/terminal
writes to validate the lease under the same locked turn row as the write. Separate
pre-dispatch checks remain useful but cannot close a lease-expiry race. This adds
one focused PostgreSQL test file within the reassessed estimate. Recovery and
preclaim-failure transitions use their own locked state guards.

1. Agree strict internal operation/evidence/assessment/draft interfaces around the
   existing public v2 types; keep public API behavior on the v2 contract.
2. Implement independently bounded read tools, validated planning/continuation,
   and grounded answer production; review each result and focused tests.
3. Wire and verify a recorded, claimed read turn through PostgreSQL, the shared
   ledger, evidence assessment and final persisted result.
4. Exercise adversarial and failure cases, run combined regression and a budgeted
   live development check, then commit the gate before Package 5.

## Required behavior

One supervisor serves shopper and merchant. Python validates registry capabilities,
mode/current permissions, entities, parameters and dependencies. Explicit user
requirements remain evidence obligations even when a model selects a shorter
template. Model-invented product IDs cannot become tool inputs. Candidates come
from the catalog or authorized context and stay bounded to five.

The Python evidence assessment records fulfilled/missing/conflicting obligations,
actual candidates, safe tool errors/retryability and remaining time/budget. A single
continuation may add at most two new reads, with at most eight expert steps and
two knowledge calls overall. Operation keys bind validated capability/parameters
and data versions. Completed results are reused; the old DAG is never re-dispatched.
Retries of a recorded non-pending turn return its existing state/result, including
interrupted turns. No model call precedes durable turn recording/claiming, and no
model/network call occurs while holding a database transaction.

`off` performs deterministic decisions and emits only checked facts/extracts.
`shadow` records model choices but follows deterministic policy. `hybrid` uses
valid model output and explicitly records fallback. `required` reports model
failure without silently replacing it with a deterministic success. All generation,
semantic verification, rewrite, embedding and retry work uses the existing ledger
and adapters, including deadline/cancellation limits.

Public answers are assembled from verified claims and citations. Structured values
come from tools and retain their subject/unit/data version; a model cannot supply a
price, page count, rating or quantity. Knowledge claims require semantic support
from the exact authorized cited source/span. Entity swaps, wrong numbers, negation,
unknown sources/spans and text outside verified claims fail closed. Lexical overlap
alone never proves support. One repair is allowed only within remaining budget;
after failure, use checked facts/extracts or explicitly abstain.

Historic catalog/review, external document and sandbox evidence remain distinct.
Until Package 5 provides offers/actions, the Package 4 read tools label historical
prices as snapshot values and expose a small service boundary for later demo-price
wiring. They never invent offers, inventory or confirmation cards. Model-selectable
planning cannot execute a write capability.

## Gate and limits

Tests cover all four modes, mixed explicit obligations, no-result/ambiguous-entity
clarification, malicious capability/ID/parameter suggestions, continuation reuse,
all hard caps, cancellation and restart/retry without duplicate dispatch. Real
PostgreSQL verifies recorded-before-provider, single claim, durable completed
steps and terminal results. Grounding tests cover forged citations, wrong entity,
number, negation, unsupported claims, semantic runtime failure and bounded repair.

The lead reviews all code/results and reruns focused plus full relevant tests.
v1 OpenAPI and historical evaluation artifacts remain unchanged. A separately
recorded live check uses development inputs only and the existing common account;
it does not establish benchmark quality. Runtime database and ledger state persist
from Package 3; they are never reset. Only a passed gate permits Package 5.

## Integrated implementation

The final runtime is split into small reviewed commits. `75c0c51` corrects decoded
Unicode author search on PostgreSQL. `99c2883` durably binds catalog/corpus/index
versions and attempted retrieval/repair counters. `0dfb5e4`, `213374a`, `399970f`
and `c9302ae` add strict runtime contracts, authorized read tools, grounding and
checked answer production. `7103085` moves active-worker lease/state checks into
the same locked transaction as step and terminal writes. `8513a3f`, `eb59550` and
`f1a2eb2` add bounded planning, durable execution and evidence-driven supervision.

The planner offers only implemented READ templates that the registry authorizes for
the current mode/scopes. A model choice must equal one Python-generated option;
product/source IDs and operation parameters remain server-owned. Explicit evidence
obligations are added back even when another valid template is selected. Price
parsing distinguishes decimal suffixes such as `1,5 triệu`, `1.5tr` and `2,5k`
from grouped VND, while an ambiguous unsuffixed decimal asks for clarification.

Every active step or terminal write rechecks the current owner and unexpired lease
under the locked turn row. Recovery requires a running turn with an expired or
missing lease, and a preclaim pin conflict can terminate only a still-pending turn.
Unexpected expert failure persists the completed deterministic read as partial
evidence before the safe terminal failure. Cancellation cleanup is best effort and
cannot replace the original `CancelledError`. A retry reads authoritative settled
and unknown usage from the stored SQL scope even when the new process has no live
provider context.

## Verification evidence

Primary ran these progressively broader gates:

- 33 supervisor/continuation tests passed in 38.66 seconds.
- 12 durable-runtime PostgreSQL tests passed in 29.37 seconds, including fresh
  replay usage, single claimant, expired recovery, crash retention and cancellation.
- 6 atomic lease-fencing PostgreSQL tests passed in 8.48 seconds.
- 126 combined Package 4 tests passed in 178.35 seconds with no skips or warnings.
- The full suite passed 588 tests with 2 optional live-v1 tests deselected and the
  known Starlette/httpx warning in 863.89 seconds.
- Ruff lint and format passed all 242 files; mypy passed 140 source files.
- Exact v1 OpenAPI comparison passed for all five v1 paths and component schemas.
- No Package 4 diff touched historical evaluation source or captured results.

The first real PostgreSQL attempt after a host restart failed before setup because
Docker was stopped. The lead recorded that failure, restored the existing loopback
Compose services, confirmed health, and reran the complete suite successfully; no
test was silently skipped or reclassified.

## Live development read and accounting

The stable `p4_catalog_off_01` turn completed and replayed with zero generation,
provider attempt, retrieval, repair, token or cost. The stable
`p4_knowledge_required_01` turn used the Sapiens development work group excluded
from held-out evaluation and the frozen Package 3 snapshots:

- corpus `cor_e06f6abbf338cfcf5fe17d450eec52ed961975e0fa8ee6bae32369ed3956`;
- index `idx_69af0802b50991c371bcb1f2954e79de82ccdc7855e15d3e602126b3b9c4`;
- catalog `cat_b9e4f648a01d78367739ce8a9b0d7670f2c9d7e7526335d9bacba50136f2baac`.

It completed with one knowledge retrieval, six generation calls, seven provider
attempts including one embedding, one repair, 5,165 input tokens, 1,738 output
tokens, 650 reasoning tokens and 6,903 total tokens. Known cost was 0.00897707 USD;
reserved/unknown costs and unknown-usage attempts were zero. The repair remained
unsupported, so the runtime marked `fallback_used` and `answer_repair_not_grounded`,
then published only the checked product-80 catalog facts and exact authorized
`src_sapiens_author` extract. Both citations retained their source/version/chunk/
span bindings.

A fresh executor inside the run and a second process using the same client-turn ID
both returned the stored terminal result and identical usage without invoking the
handler or increasing ledger cost. The common account moved from 52,640 to
9,029,710 nano-USD known usage, with zero reserved/unknown usage. Baseline and
runtime databases remain revisions 0003 and 0007, each with 200 products and 1,773
reviews. This is an end-to-end development safety check, not benchmark evidence.

## Gate decision

Package 4 passes. The checked read path records before dispatch, respects all hard
caps, reuses completed work, fails closed on authorization/grounding faults, emits
only supported claims, and accounts for provider work. Package 5 added durable
history, explicit memory and sandbox actions. The later endpoint consolidation
retired `/api/v1`; current routes are documented in [API v2](../api.md).
