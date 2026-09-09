# Package 3 — versioned book corpus and retrieval

Package 2 passed and was committed at `16f0d2a` before this package started. This document records the implementation boundary and independently verified runtime artifacts. The package gate is recorded separately in the progress log after the complete regression run.

## Scope and ownership

The three independent scopes are public source curation and complete 200-book mapping (Sol XHigh), corpus ingestion/PostgreSQL storage (Sol XHigh), and hybrid retrieval with stable citations (Sol Max). The initial estimate is 12–18 source/test files plus compact curated data/manifests, provenance/license and documentation. The lead reviews shared typed interfaces before the components are wired together and reassesses any expansion.

The curator receives only book IDs, titles, authors, publisher, page count and available ISBN/language/publication-year metadata. Reviews, raw source archives, user data and credentials are excluded. Every mapping is classified as `exact_edition`, `exact_work`, `ambiguous` or `unmatched`; publication requires sources supporting at least 20 distinct works. Unresolved edition information never becomes an exact-edition claim. Sources with ambiguous identity are not published.

Only verified public sources enter the corpus. Each records URL, retrieval time, basis for use, content hash, version and relation to a work/edition. If verbatim reuse is not permitted, the corpus uses bibliographic metadata and independently written factual notes attributed to the source. No benchmark questions/answers or unlicensed full-book text enter the corpus.

## Runtime and reproducibility boundaries

The runtime database is `thanh_v2_runtime` on the separate loopback PostgreSQL service at `127.0.0.1:55432`. It contains the quality-gated 200-product historical snapshot, initially at revision 0005 and now at 0006 after the reviewed migration `2424a68`. The lead verified that its 200 products, 1,773 reviews and shared budget account `thanh-v2-provider-global` were preserved. Account totals must survive retries, restarts and later work packages. The original `ecommerce_p1` baseline remains at revision 0003; temporary test databases remain separate.

The lead alone runs reviewed live ingestion with the existing configured OpenAI key, through the shared attempt ledger. Corpus work has an initial allocation of 5 USD within the overall 100 USD limit. An ingestion scope begins immediately before provider work so setup time cannot silently consume its deadline. Offline hashing validates deterministic mechanics only; the published operational index uses the approved `text-embedding-3-small`, 1,536-dimensional boundary, and every attempt is accounted for.

Documents, chunks, vectors and mapping records use immutable corpus/index identities. The index fingerprint includes embedding model, dimensions, chunker version and enrichment policy. A new fingerprint requires new vectors even when document content is unchanged. A turn pins one published corpus version. No Qdrant, Claude runtime, new agent framework or second provider loop is introduced.

SQL authorization filters precede model rewrite, source selection and ranking; subsequent validation provides an additional check. Grants derive from current server authorization. Model-supplied IDs may narrow permitted sources but cannot expand them. Retrieval uses BM25 and dense candidates combined by weighted RRF, with bounded duplicate removal and adjacent context. Defaults are 6 chunks, maximum 8 including expansion, and at most 6,000 retrieval-context tokens. Table rows repeat headers and obey the same per-chunk cap.

Relevance checks allow an empty result; a high relative rank alone does not establish that the source answers the question. Development-only threshold calibration is recorded and frozen before the official benchmark. Evidence IDs bind source, version, chunk and span; `[C1]` is a display label. Semantic claim verification is implemented in Package 4, using the shared runtime.

## Gate

The lead verifies the source/edition mapping and license provenance, publication of a valid version covering at least 20 works, complete 200-record mapping, stable IDs and rebuild behavior, separate vectors for changed fingerprints, table limits, SQL ACL before planner/ranking, no-answer behavior, authorized citation reopening and exact spans. Tests exercise the PostgreSQL boundary on real isolated databases. The package also checks the common budget ledger and v1 regression. Only a passed gate permits Package 4; verified slices are committed separately following `commit.md`.

## Reviewed progress and necessary schema correction

Contracts are committed at `5fb0994`; the curated 20-work/200-record source inputs
are committed at `a1edfff`. The primary independently validated both slices. The
input files supplied the operational publication recorded below.

The existing chunk uniqueness `(document_id, chunk_index)` is insufficient when
an immutable index changes its chunker. Package 3 therefore adds an explicit
`chunker_version` column to the uniqueness key through a new migration. Existing
data is preserved; an ambiguous backfill or a downgrade that would collapse two
chunk layouts must fail instead of discarding content. This correction supports
the planned index fingerprint invariant without changing document identity.

The migration slice passed the lead's 21-test SQLite/PostgreSQL migration and
persistence run. Validation happens before schema mutation, and downgrade also
refuses a chunker identity that could not be reconstructed on reapply. These
checks establish the schema boundary; ingestion and retrieval still require
their integrated gate before the package can close.

## Development relevance probes

The eight fixed queries and expected sources are recorded in
[development-probes.json](../../data/knowledge/books-v1/development-probes.json).
They were selected before any live query-embedding or retrieval output. The lead
checked every query against the earlier plan, whose SHA256 is
`8f365b18451486245242e94b5d125e14c7b24713ff4a48ca7f3b2c2007cfe4a4`.
The canonical JSON hash of the probe artifact is
`c1fd1801dc2481384ca8383f21eb140c2ee36238906c7ea294c1d544bfce3433`.

Products 80, 168, 179 and 101 and their entire work groups are development-only.
The four negative query families and their paraphrases are also excluded from
Package 7's held-out partition. Gold source IDs will not be relabelled after
observing retrieval. These probes measure relevance and abstention, not semantic
claim support or benchmark superiority. Query vectors will be collected once
through the shared ledger and reused for offline policy comparisons. The final
selected policy remains provisional until the Package 7 protocol freeze.

## Reviewed ingestion and calibration commands

`scripts/build_book_corpus.py` reads the committed source and mapping manifests,
validates all 200 current catalog IDs and builds a new immutable PostgreSQL
corpus/index. It requires an existing ingestion budget scope capped at 5 USD in
the common account. The command never creates or resets the ledger. Credentials
come only from the named API-key environment variable.

`--build-owner-id` stays fixed across safe resumes, including a new budget scope
after the original deadline. Completed batches are reused. An active batch with
an unpersisted outcome is refused for operator reconciliation; changing scope or
owner cannot silently dispatch it again. `--retrieval-policy` reads a strict
policy JSON. `--corpus-version` revalidates the same override in both input
manifests, allowing a calibrated policy to publish a new version while reusing
unchanged same-fingerprint vectors. The original published version remains
immutable. Multiple complete indices require an explicit index ID for reads.

`scripts/calibrate_book_retrieval.py collect` takes explicit corpus/index IDs and
server-side access-fixture fields. It collects the fixed probes' deduplicated
query vectors in one budgeted batch. An exclusive pending claim precedes dispatch;
the complete cache records input identity, vector digest and known/reserved/unknown
usage. A complete payload remains reusable even if an earlier retry has unknown
billing. An incomplete payload never triggers automatic redispatch.

The `evaluate` subcommand uses the complete cache and current PostgreSQL ACLs with
zero provider calls. It records raw ranks/relevance/selection, expected sources,
no-answer and context bounds for each policy. Collection and evaluation snapshot
lineage remain separate if a new corpus version reuses the query vectors. The
runner does not mutate a published policy or claim semantic grounding. `--help`
lists the required arguments and bounded collect/evaluate examples.

Primary verification: 31 focused tests passed, including six real PostgreSQL
scenarios; lint/format and typed source checks passed. These fixture tests do not
establish live embedding quality or publication. Final runtime IDs, policy,
calibration results and complete ledger usage follow only after those checks run.

## Published runtime and observed calibration

The primary published `books-v1-calibrated-20260909` with corpus ID
`cor_e06f6abbf338cfcf5fe17d450eec52ed961975e0fa8ee6bae32369ed3956`
and index ID
`idx_69af0802b50991c371bcb1f2954e79de82ccdc7855e15d3e602126b3b9c4`.
Its index fingerprint is
`ee3afa43eb9199f0b962f467946e54127c7133424b9f56d84660975a5df34759`.
The index uses `text-embedding-3-small`, 1,536 dimensions, `table_chunker_v1`
and `source_keywords_v1`. There are 20 sources/chunks/vectors and all 200
product mappings: 20 exact work, 17 ambiguous and 163 unmatched.

The six-policy development grid found 4/4 expected positive sources and 4/4
correct negative abstentions at dense relevance 0.25. Both lexical thresholds
separated the probes; the selected policy retains the stricter existing 0.10.
Positive queries still return extra sources. These observations do not establish
exact-source precision, semantic support or held-out quality. The selected policy
remains provisional until Package 7 freezes the complete protocol.

The calibrated publication reused all 20 vectors from the original published
`books-v1` corpus. Primary compared their public chunk IDs and vector payloads
directly and verified that publishing the new version made zero provider calls.
The direct published-store audit then ran all eight queries from the cache and
reopened all 24 returned evidence references with matching text and timestamp.
A foreign tenant received no context. The audit made zero provider calls.

The complete evidence is committed in:

- [query-embeddings.json](../../data/knowledge/books-v1/calibration/query-embeddings.json): immutable one-batch query cache and usage.
- [candidates-v1.json](../../data/knowledge/books-v1/calibration/candidates-v1.json): raw diagnostics for all six policies.
- [selected-policy.json](../../data/knowledge/books-v1/calibration/selected-policy.json): the published retrieval policy.
- [published-policy-verification.json](../../data/knowledge/books-v1/calibration/published-policy-verification.json): policy-evaluation result with separate collection/evaluation lineage.
- [runtime-manifest.json](../../data/knowledge/books-v1/runtime-manifest.json): actual publication, input/vector fingerprints, direct facade/citation checks and ledger totals.

The policy runner is read-only even when its candidate equals the published
policy. Actual publication and direct facade behavior were independently checked
in the runtime manifest. Calibration/gold artifacts are verification inputs only;
they must never be injected into application model context or the ingested source
corpus.

Total observed Package 3 provider use is **2 embedding attempts, 2,632 input
tokens, 52,640 nano-USD (0.00005264 USD)**. The corpus build accounts for 2,159
tokens and query collection for 473. There are no pending attempts, unknown costs,
missing usage or held reservations. No generation, warmup or benchmark request was
made. The baseline database remains revision 0003 with 200 products/1,773 reviews;
the runtime is revision 0006 with the same catalog counts. Neither database nor
the common account was reset.

## Final gate

Primary full regression: **462 passed, 2 optional live v1 tests deselected,
1 existing Starlette warning**, with all PostgreSQL checks executed. Ruff passed
for 202 files, mypy for 133 application modules, and all five v1 OpenAPI routes and
component schemas are unchanged. The unified verifier reported
`VERIFY_STATUS=SUCCESS`; its generic config smoke was marked skipped because the
explicit provider/publication/PostgreSQL/citation audits above supply runtime
verification. The gate is met; semantic grounding remains the next package.
