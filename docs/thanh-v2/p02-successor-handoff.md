# P02 remediation and successor execution status

This record supersedes the unstarted `31f3351` successor handoff. Live egress
was authorized for the historical successor lifecycle and a local PostgreSQL
runtime was prepared on its dedicated loopback database. P7 successor v5
completed and froze its repeat decision. P8 v6-v9 produced terminal partial
checkpoints; v11 later completed all 720 SUT cells but its `operate` calibration
failed under an outdated judge schema. No score or publication is claimed.

Current continuation status (2026-10-02): P7 successor `run_p7_successor_v10`
completed 36/36 cells without failures on protocol
`36ac8fec094b201345d74a324aa569057cb1dae5c506cd78763ee327ad5f983c`. Repeat
decision `9b4a7552919e687bdfe57a67f144571d26daf1c0ffbbcd432dc721411386c3df`
froze 3 repeats; projected held-out SUT cost is `$5.97439800`. P7 v5
(`28621f…`), v8 (`a249d0…`) and partial v9 are historical. The user has approved
both Responses and Embeddings API requests for P7/P8.

P8 v14 completed 720/720 SUT cells, but `operate` stopped when it could not
resolve exact `trust` citations; it produced no judged score. P8 v15 is retained
as a partial after sandbox outbound networking was blocked: 213 completed, 82
failed, and 425 not started. Official successor `run_p8_successor_v16` completed
720/720 cells with zero failed, missing, ambiguous, pending or orphan work under
schedule SHA
`5c7930f734fb29ede24ef04c3cfec64c7c3fd70bc241a76f8ed93918a63ddb25`.
`operate` passed exact evidence resolution and generated 32/32 development
calibration records, but the frozen max-absolute-error threshold `0.25` failed
for all six semantic metrics (maximum error `1.0` each). Calibration did not
freeze; blind judging, final score, paired metrics and report were not produced.
Outputs remain in `output/evaluation-v3/heldout-successor-v16/`.

The PostgreSQL ledger is isolated from the checkout `.env` (which still points
to SQLite). Diagnostic A's `$0.05446350` was identified in its manifest as
belonging to clone `p8_failed_diag_20260923_a`, but its original SQL settlement
rows are unavailable; the amount remains in account-level unknown usage.
The 2026-10-02 02:07 UTC snapshot records `$19.60072061` known, `$0.32514984`
unknown, `$0` reserved/active/pending and `$80.07412955` remaining under the
`$100` cap. Unknown includes carry-forward `$0.18567534`, P8 v15 `$0.13102500`
and a P8 v16 judge timeout `$0.00844950`. P7 v10 cost `$0.05652510` matches its
34 database attempts; P8 v16 SUT cost `$1.47381435` matches 768 known attempts.
The ledger reconciliation is
`output/evaluation-v3/current-machine-ledger/ledger-reconciliation-20261002.json`.
GitHub Actions run `36844660998` failed at Type-check on pushed SHA
`470d1817ccb8c2294dec7aace9319a69e2c93d78`. Commit `802369b` fixes the eight
mypy errors; full CI run
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

The P8 protocol guards bind to current P7 protocol
`36ac8fec094b201345d74a324aa569057cb1dae5c506cd78763ee327ad5f983c`. P8 v11
used the previous judge schema; v12-v15 are historical or partial. Current
official run is v16. The v5 protocol/repeat decision is not reused.

Fresh Windows checkouts previously changed immutable source hashes. Git attributes
now preserve LF text, except `sources.json` and `mappings.json`, whose already
frozen bindings require CRLF. Gold, split and source JSON content are unchanged.
The OpenAI adapter also has a type-only cast for the pinned SDK's TypedDict return;
runtime payloads and dispatch behavior are unchanged.

## Successor execution record

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
citations as catalog citations, or soften a provenance failure. P7 v10 is frozen;
P8 v16 has complete SUT coverage and exact evidence resolution. The calibration
gate remains open because its frozen error threshold failed, so no blind judging
or publication can run under the current frozen configuration.

On any failure, classify the error and inspect its checkpoint before changing
code. Fix only the demonstrated cause; preserve completed cells and bind any
source change to a distinct compatible run. P7 is frozen and P8 SUT coverage is
complete, but final P8 quality, blind scoring, paired metrics and package closure
remain pending resolution of the failed calibration gate.

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
verification. P7 v5/v9 remain historical; current P7 v10 is frozen. P8 v11/v14
have complete SUT checkpoints but no final result; v16 is the current full SUT
run and has no final result because calibration did not freeze.
