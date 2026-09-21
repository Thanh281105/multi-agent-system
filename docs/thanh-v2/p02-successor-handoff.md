# P02 remediation and deferred successor execution

This record supersedes the unstarted `31f3351` successor handoff. The operator
explicitly deferred credentials, PostgreSQL and live P7/P8 execution in this
session. No benchmark prompts were sent to OpenAI. No pilot checkpoint, repeat
decision, held-out run or judging report was created.

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

The current protocol additionally binds supervisor and grounding source files:

- Protocol: `315efff0d596f11ea63aa60729834e54fb5d6acb9bf6b7d2b9260b96b601451f`.
- Planned run: `run_p7_successor_v4`.
- Planned output: `output/evaluation-v3/pilot-successor-v4`.
- Schedule: `754b168a7d361986c98c243b3fbd7e69fbdbf3c7dd4177e0d90f5f49492f923e`.
- Local dry-run: 36 cells, 4 warmups, 32 measurements, 0 network calls.

The three P8 protocol guards are rebound to this exact source protocol. No P8
run is created by that code change. No previous repeat decision is reused.

Fresh Windows checkouts previously changed immutable source hashes. Git attributes
now preserve LF text, except `sources.json` and `mappings.json`, whose already
frozen bindings require CRLF. Gold, split and source JSON content are unchanged.
The OpenAI adapter also has a type-only cast for the pinned SDK's TypedDict return;
runtime payloads and dispatch behavior are unchanged.

## Resume checklist after credentials and PostgreSQL are configured

1. Obtain explicit approval for sending benchmark prompts and judging inputs to
   OpenAI, with the intended run IDs and budget. Do not print `.env`, API keys or
   database credentials. Run PostgreSQL transactional/replay gates first on the
   dedicated `TEST_POSTGRES_URL` server, and prepare the live database/corpus.
2. Inspect existing output/checkpoints before dispatch. An existing terminal cell
   must never be redispatched. If another machine has already started v4 under
   the old source, preserve it and allocate a new run/output instead of rebinding
   its checkpoint. The CLI must reject source/checkpoint mismatches.
3. Run the local preflight:

   ```powershell
   python -m app.evaluation.v3_cli validate
   python -m app.evaluation.v3_cli dry-run --run-id run_p7_successor_v4
   ```

4. After explicit egress approval, start `v3_cli pilot` with
   `--run-id run_p7_successor_v4`, `--output output/evaluation-v3/pilot-successor-v4`,
   `--allow-network` and `--database-url $env:DATABASE_URL`. Use `resume` only for
   an existing compatible checkpoint. Freeze only after all 36 cells are terminal
   and the freeze validator accepts the receipt/ledger evidence.
5. Only after complete/frozen P7, allocate `run_p8_successor_v4` and
   `output/evaluation-v3/heldout-successor-v4` (or another unused pair). Supply
   `--p7-protocol` and `--p7-repeat-decision` from that same P7 output. Require
   `60 × 4 × frozen_repeats` receipt cells (`480` or `720`), complete
   attempt/retry/warmup/embedding accounting and zero
   pending/ambiguous/orphan/failed cells. Do not force a three-repeat decision
   if the frozen cost gate does not permit it. Never resume
   `run_p8_heldout_corrected_v3` to replace settled cells.
6. For `benchmark_cli operate`, also pass `--pilot-checkpoint` and
   `--pilot-schedule` from the same P7, plus explicit judge budget. Resolve exact
   evidence, calibrate, freeze judge bindings, blind-judge and publish only when
   all validators succeed.

The exact evidence boundary is now receipt-bound and fail-closed.
`ImmutableBenchmarkEvidenceResolverV3` reopens knowledge, canonical
merchant-inventory and shopper cart/checkout preview reads reconstructed from
the hashed reset fixture, and `catalog_product_N`/`review_sample_N` records
reconstructed from hash-pinned public assets. Ranked catalog records, oversized
review samples, and mutated sandbox state still stop scoring with a typed error.
Do not fabricate source text from the answer, title or gold, relabel sandbox
citations as catalog citations, or soften a provenance failure. This does not
make `operate` ready to complete: successor P7 must still run and freeze before
the P8 lifecycle can start.

On any failure, classify the error and inspect its checkpoint before changing
code. Fix only the demonstrated cause; preserve completed cells and bind any
source change to a distinct compatible run. Final P7/P8 quality and final package
closure remain pending live evidence and PostgreSQL gates.

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
(`5` and `46` tests respectively); the broader API v2/action/replay matrix and
live P7/P8/operate remain deferred by the operator. They must not be represented
as passed or complete.
