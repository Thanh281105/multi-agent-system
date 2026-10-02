# P02 remediation and successor execution status

This handoff records the completed P7/P8 successor lifecycle. P7 successor
`run_p7_successor_v18` completed 36/36 cells with no failures and froze 3 repeats
on protocol SHA-256
`2d5f2afface0dd5c1337d52d67bf0b3cca6c66328cfbdf71fa77c58659691d23`; repeat
decision SHA-256 is
`40ea5ec8a1890c34195b8e3f4fd29935203834985625605af92fa24bbbd8c5cd`.

P8 successor `run_p8_successor_v23` completed 720/720 SUT cells under schedule
SHA-256 `f46949cdae870718dd8d2411a343522842eb34c466554e5350ae960ea7e8892e`.
Exact evidence resolution, 32/32 calibration records, judge freeze, 720 blind
judgments, result joining, report and paired metrics all completed. Calibration
SHA-256 is
`8176e307bd5a34610f2855e71777f46b708058823621120b1ec209993e1f9031`; report
SHA-256 is
`cf772ac876e0a2f69aef78f68ffa729925eb991359b76bc94da4f26fc6787f5d`.
Publication files are under
`output/evaluation-v3/heldout-successor-v23/p8-publication/`; these benchmark
artifacts are local-only.

The PostgreSQL ledger is isolated from the checkout `.env` (which still points
to SQLite). The final `codex-p7-p8-ledger` snapshot records `$41.57320211` known,
`$0.47555784` unknown, `$0` reserved/active and `$57.95124005` remaining under
the `$100` cap. Current SQL scopes account for `$0.40455225` unknown; the
remaining `$0.07100559` is account-level carry-forward without original scope
rows. Diagnostic A's `$0.05446350` belongs to historical clone
`p8_failed_diag_20260923_a` and is included within that carry-forward. Its SQL
rows are unavailable, so the amount remains unknown and is not added twice.
P7 v18 known cost is `$0.05163060`. P8 v23 known provider usage is
`$4.90357425`; published effective cost is `$4.91358675`, including the report's
`$0.01001250` unresolved-reservation estimate. This estimate is not an active
PostgreSQL reservation. No P7/P8 Embeddings API attempts were recorded; the
three embedding attempts in this database belong to ingestion.

The local-only reconciliation is
`output/evaluation-v3/current-machine-ledger/ledger-reconciliation-20261002-v23-final.json`.
Diagnostic A's original clone remains unavailable and no active reservation is
left in the current account. Earlier CI history: run `36844660998` failed at
Type-check on SHA `470d1817ccb8c2294dec7aace9319a69e2c93d78`; commit `802369b`
fixed the eight mypy errors. Full CI run
[`36846265660`](https://github.com/Thanh281105/multi-agent-system/actions/runs/36846265660)
passed on SHA `802369b263b19da7ec4388605ef38cd19da2bc2f`. Documentation commit
`1331897` is pushed, and full CI run
[`36848384922`](https://github.com/Thanh281105/multi-agent-system/actions/runs/36848384922)
passed on SHA `13318976919d349be73189194f08d354715761ab`.

## Implemented boundary

`PlanningContext.merchant_target` carries the server-selected product, offer and
expected version independently of `resolved_product_ids`. Multiple products
without this target still require clarification. The evaluation adapter projects
only that target from the hash-bound sandbox fixture and namespaces its offer ID;
it does not supply the expected answer or proposal price to the planner. The
requested price continues to be parsed from the user message.

Combined merchant requests execute `merchant.inventory.read` for every resolved
product through the durable operation executor, require a grounded demo price
for every returned offer, and only then call the existing proposal service.
The offer is matched by the resolved product and checked against the server's
offer ID/version. Product order, offer order and model-selected facts cannot
choose the mutation target. Missing products/prices, failed reads, wrong targets
and stale versions cannot produce a proposal. The existing confirmation,
authorization and transaction/version checks still apply; no execute capability
is added. Successful responses preserve claims, citations and read execution
records alongside the confirmation card.

Grounding now recognizes only the three inventory fields emitted by the tool:
`demo_price_vnd`, `demo_stock`, and `demo_offer_version`, with strict integer,
unit, range and sandbox-evidence checks. These are labeled demo inventory facts,
not fabricated catalog snapshot facts.

The ordinary API must supply an independently resolved server target to use the
multi-product path. This change does not add a client-controlled target field or
a model-based entity resolver. Offline behavior tests do not establish live gold
quality or PostgreSQL transaction/replay correctness.

## Source bindings and dry-run

The old protocol `c8a4b4fb912039b93071fa37030cdbb11971cf3a15c65000d7c7e2ddfff79cde`
and schedule `f37eaa4482cafa85b9c665bf6ea25f30a81872df277e3e803949035209b7da13`
describe the pre-P02 source. They must not be used for this implementation.

The historical v5 protocol additionally binds the shopper fixture guard and
the supervisor/grounding source files:

- Protocol: `28621f0e6b7c1e8377b5b97bd9ebd7287054b58367979d00f558473d788f040c`.
- Frozen P7 run: `run_p7_successor_v5`.
- Output: `output/evaluation-v3/pilot-successor-v5`.
- Schedule: `82617ce0e4004fcda0b542a1769de9653be044643f5189a5b3e88fa0aebd2163`.
- Pilot: 36/36 terminal cells, 4 warmups, 32 measurements, 0 failed cells.
- Repeat decision: 3 repeats; pilot effective cost `0.05551620 USD`, projected
  held-out SUT cost `5.91510600 USD`.

In the historical v11 snapshot, the P8 protocol guards bound to P7 v10 protocol
`36ac8fec094b201345d74a324aa569057cb1dae5c506cd78763ee327ad5f983c`. P8 v11
used the previous judge schema; v12-v15 are historical or partial. P8 v16 was
the next official run in that snapshot. Both were later superseded by P7 v18 and
P8 v23, documented at the top of this handoff. The v5 protocol/repeat decision
is not reused.

Fresh Windows checkouts previously changed immutable source hashes. Git attributes
now preserve LF text, except `sources.json` and `mappings.json`, whose already
frozen bindings require CRLF. Gold, split and source JSON content are unchanged.
The OpenAI adapter also has a type-only cast for the pinned SDK's TypedDict return;
runtime payloads and dispatch behavior are unchanged.

## Historical successor execution record (through P8 v16)

1. Preflight validated the corrected protocol and the exact 36-cell P7 v5
   schedule. The live pilot was dispatched with explicit network consent and a
   loopback PostgreSQL URL, then repeat-freeze accepted the receipt and ledger
   evidence.
2. P8 runs v6, v7, v8 and v9 each used a distinct append-only output/checkpoint
   and the same frozen P7 protocol/repeat decision. Their terminal summaries are:

   | Run | Schedule | Completed | Failed/missing |
   | --- | --- | ---: | ---: |
   | `run_p8_successor_v6` | `c0c41ae8…` | 718 | 2 |
   | `run_p8_successor_v7` | `ad5b8d57…` | 716 | 4 |
   | `run_p8_successor_v8` | `7aae9668…` | 714 | 6 |
   | `run_p8_successor_v9` | `9efb06ceb5843f069f1e84e6f099ff57c5103b6a0167114db52e09d0bc24b394` | 712 | 8 |

   All have zero ambiguous/orphan cells. The v9 failures are one timeout, two
   `expert_selection_not_authorized`, two `model_response_incomplete` and three
   `model_response_invalid`; the shopper merchant-target binding failure from
   the older protocol no longer appears.
3. Existing terminal cells were never redispatched. `resume` cannot repair a
   terminal partial checkpoint. P8 v11 did have complete SUT coverage, but
   `operate` failed calibration: four model responses violated semantic schema,
   then 28 jobs were stopped by the circuit breaker. The corrected schema
   changes the protocol, so v11 cannot be carried forward.
4. The one-case diagnostic isolated the cause: the old provider JSON schema
   admitted nine metric names while the semantic validator accepted six. The
   schema-only fix is committed as `d030f9a`; a known-cost retry batch was
   `$0.02340150`, and the diagnostic scope was `$0.00588450`. Eight earlier
   sandbox-blocked attempts remain historical unknown at `$0.06824250`; P7 v9's
   eight connection-failed attempts remain historical unknown at `$0.03670500`.
   P7 v10 has since completed and frozen the repeat decision. P8 v16 now has
   full SUT coverage and exact evidence resolution, but calibration failed its
   frozen threshold; blind judging and report publication did not run.

The exact evidence boundary is now receipt-bound and fail-closed.
`ImmutableBenchmarkEvidenceResolverV3` reopens knowledge, canonical
merchant-inventory and shopper cart/checkout preview reads reconstructed from
the hashed reset fixture, plus `catalog_product_N`, `review_sample_N` and
`trust_sample_N` records reconstructed from hash-pinned public assets. Trust
samples are accepted only when their full review set fits the runtime limit.
Ranked catalog records, oversized review samples, and mutated sandbox state
still stop scoring with a typed error.
Do not fabricate source text from the answer, title or gold, relabel sandbox
citations as catalog citations, or soften a provenance failure. At the v16
checkpoint, P7 v10 was frozen and P8 v16 had complete SUT coverage and exact
evidence resolution, but calibration failed and no blind judging ran. That
checkpoint is historical; P7 v18/P8 v23 completed the lifecycle as recorded at
the top of this handoff.

On any failure, classify the error and inspect its checkpoint before changing
code. Fix only the demonstrated cause; preserve completed cells and bind any
source change to a distinct compatible run. The v10/v16 statements in this
execution record describe the earlier checkpoint and are superseded by the
completed v18/v23 results above.

## Local verification

Focused supervisor, grounding, executor and P8 evidence/protocol/calibration/judge
regression: 155 passed. Additional P02 target/grounding adversarial tests and
executor tests: 23 passed. Frontend: 116 tests passed; lint, TypeScript and Vite
build passed. API v1/gateway/security/health/operations regression: 41 passed.
Documentation/deployment regression: 18 passed after correcting the pre-existing
entrypoint expectation (missing the v3 CLI) and making the README's historical
snapshot wording explicit. Ruff lint/format, mypy (169 source files) and
`git diff --check` pass. The broad offline run reported 785 passed, 2 failed,
1 skipped; those two pre-existing documentation/entrypoint failures were fixed
and rerun successfully (2 passed). Thus all 787 selected runnable tests have
passing verification, with 19 PostgreSQL-dependent modules explicitly excluded.
This is an initial run plus focused rerun, not a claim of one clean full-suite run.

Targeted PostgreSQL executor and v2 action/sandbox seed gates passed locally
(`5` and `46` tests respectively). A subsequent 243-test v2 API/supervisor
selection ended with 2 failures and 14 setup errors solely because
`TEST_POSTGRES_URL` was unset; that run is not a pass. Re-running the
database-dependent cases against a loopback disposable PostgreSQL URL passed 17
tests with 1 existing Starlette/httpx warning. An additional PostgreSQL v2
action/history/lease/persistence/runtime/tools/sandbox suite passed 103 tests.
Together these local results close the broader API v2/action/replay PostgreSQL
verification. P7 v5/v9 and P8 v11/v14/v16 remain historical checkpoints. The
current frozen pilot is P7 v18; P8 v23 has complete SUT coverage, frozen
calibration, blind judgments, and published report/paired metrics as recorded
above.
