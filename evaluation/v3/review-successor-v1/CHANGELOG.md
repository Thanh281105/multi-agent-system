# Review successor inputs v1

Base: `evaluation/v3/{experiment,gold,split}.v3.json`, audited 03/10/2026.
Original files remain unchanged. This version keeps 20 development and 60
held-out conversations, the same 27 held-out work groups, four variants,
all 264 required facts and exact source supports. No expected answer or
response mode was derived from a successor system answer.

The 16 `multi_constraint` conversations request finding a named title with
price/rating constraints and supported work knowledge. Their required read
capabilities are now `product.catalog.search` and `knowledge.retrieve`.
`product.rank` remains allowed, but is no longer required or asserted as an
attempt: these requests do not ask for a ranking and a redundant rank call is
not evidence of a valid plan. Other capability and action boundaries are
unchanged. This is a contract correction, not a relaxation of required facts,
numeric matching, authorization or claim support.

Corpus/experiment identifiers and split gold hash are versioned. The split
schema discriminator remains `evaluation_v3_frozen_split`. Loaders validate
registry, source hashes, exact spans, roster, quotas and evidence count.
Evaluator successor uses `evidence_semantics_v2`, independent reference labels
and explicit protocol/repeat bindings; historical evaluator behavior remains
read-compatible.

This roster has been reused for engineering/debugging and is not untouched
external evidence. A separately locked challenge set and stratified human
adjudication must be reported independently. Automated Codex/model reference
labels must never be presented as human judgments.

Validated canonical hashes before runtime freeze:

- Gold: `f698841509a1cb95cc610af2830e6d5f91ba266dd72587c0e127793ccce9b59c`.
- Split: `d4af32e7aebf5144d43f443fbafe64ecd043df18eac2dcac4f062e6a7fb1381f`.

Protocol/source hashes and repeat decision are frozen only after source checks.
