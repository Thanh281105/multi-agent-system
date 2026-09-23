# P02 remediation and successor execution status

This record supersedes the unstarted `31f3351` successor handoff. Live egress
was authorized for the successor lifecycle and the local PostgreSQL runtime was
prepared on the dedicated loopback database. P7 successor v5 completed and
froze its repeat decision. P8 successors v6 through v9 then produced terminal
partial checkpoints; no scoring, calibration, judging or publication report is
claimed because every held-out run still has fail-closed cells.

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

The corrected protocol additionally binds the shopper fixture guard and the
supervisor/grounding source files:

- Protocol: `28621f0e6b7c1e8377b5b97bd9ebd7287054b58367979d00f558473d788f040c`.
- Frozen P7 run: `run_p7_successor_v5`.
- Output: `output/evaluation-v3/pilot-successor-v5`.
- Schedule: `82617ce0e4004fcda0b542a1769de9653be044643f5189a5b3e88fa0aebd2163`.
- Pilot: 36/36 terminal cells, 4 warmups, 32 measurements, 0 failed cells.
- Repeat decision: 3 repeats; pilot effective cost `0.05551620 USD`, projected
  held-out SUT cost `5.91510600 USD`.

The three P8 protocol guards are rebound to this exact source protocol. No P8
run is created by that code change. No previous repeat decision is reused.

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
   terminal partial checkpoint, and no run is eligible for `operate` until every
   scheduled receipt cell is complete and the ledger has no unresolved work.
4. `benchmark_cli operate` remains intentionally uncalled. It still requires
   the same P7 checkpoint/schedule, exact evidence resolution and explicit judge
   budget, and it may publish only after calibration, blind judging and all
   validators succeed.

The exact evidence boundary is now receipt-bound and fail-closed.
`ImmutableBenchmarkEvidenceResolverV3` reopens knowledge, canonical
merchant-inventory and shopper cart/checkout preview reads reconstructed from
the hashed reset fixture, and `catalog_product_N`/`review_sample_N` records
reconstructed from hash-pinned public assets. Ranked catalog records, oversized
review samples, and mutated sandbox state still stop scoring with a typed error.
Do not fabricate source text from the answer, title or gold, relabel sandbox
citations as catalog citations, or soften a provenance failure. This does not
make `operate` ready to complete: successor P7 is frozen, but P8 must first
reach complete SUT coverage before evidence, calibration, judging and
publication can run.

On any failure, classify the error and inspect its checkpoint before changing
code. Fix only the demonstrated cause; preserve completed cells and bind any
source change to a distinct compatible run. Final P7/P8 quality and final package
closure remain pending live evidence and the successor lifecycle gates.

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
verification. P7 is complete and frozen; P8 SUT is terminal partial and
`operate` remains blocked by the missing/failed cells above.
