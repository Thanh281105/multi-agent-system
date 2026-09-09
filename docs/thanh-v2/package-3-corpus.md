# Package 3 — versioned book corpus and retrieval

Package 2 passed and was committed at `16f0d2a` before this package started. This document records the active implementation boundary; it does not claim that a production corpus or retrieval gate is complete.

## Scope and ownership

The three independent scopes are public source curation and complete 200-book mapping (Sol XHigh), corpus ingestion/PostgreSQL storage (Sol XHigh), and hybrid retrieval with stable citations (Sol Max). The initial estimate is 12–18 source/test files plus compact curated data/manifests, provenance/license and documentation. The lead reviews shared typed interfaces before the components are wired together and reassesses any expansion.

The curator receives only book IDs, titles, authors, publisher, page count and available ISBN/language/publication-year metadata. Reviews, raw source archives, user data and credentials are excluded. Every mapping is classified as `exact_edition`, `exact_work`, `ambiguous` or `unmatched`; publication requires sources supporting at least 20 distinct works. Unresolved edition information never becomes an exact-edition claim. Sources with ambiguous identity are not published.

Only verified public sources enter the corpus. Each records URL, retrieval time, basis for use, content hash, version and relation to a work/edition. If verbatim reuse is not permitted, the corpus uses bibliographic metadata and independently written factual notes attributed to the source. No benchmark questions/answers or unlicensed full-book text enter the corpus.

## Runtime and reproducibility boundaries

The initial runtime database is `thanh_v2_runtime` on the separate loopback PostgreSQL service at `127.0.0.1:55432`. It contains the quality-gated 200-book snapshot at schema revision 0005. Its shared budget account is `thanh-v2-provider-global`; account totals must survive retries, restarts and later work packages. The original `ecommerce_p1` baseline and temporary test databases remain separate.

The lead alone runs reviewed live ingestion with the existing configured OpenAI key, through the shared attempt ledger. Corpus work has an initial allocation of 5 USD within the overall 100 USD limit. An ingestion scope begins immediately before provider work so setup time cannot silently consume its deadline. Offline hashing validates deterministic mechanics only; the published operational index uses the approved `text-embedding-3-small`, 1,536-dimensional boundary, and every attempt is accounted for.

Documents, chunks, vectors and mapping records use immutable corpus/index identities. The index fingerprint includes embedding model, dimensions, chunker version and enrichment policy. A new fingerprint requires new vectors even when document content is unchanged. A turn pins one published corpus version. No Qdrant, Claude runtime, new agent framework or second provider loop is introduced.

SQL authorization filters precede model rewrite, source selection and ranking; subsequent validation provides an additional check. Grants derive from current server authorization. Model-supplied IDs may narrow permitted sources but cannot expand them. Retrieval uses BM25 and dense candidates combined by weighted RRF, with bounded duplicate removal and adjacent context. Defaults are 6 chunks, maximum 8 including expansion, and at most 6,000 retrieval-context tokens. Table rows repeat headers and obey the same per-chunk cap.

Relevance checks allow an empty result; a high relative rank alone does not establish that the source answers the question. Development-only threshold calibration is recorded and frozen before the official benchmark. Evidence IDs bind source, version, chunk and span; `[C1]` is a display label. Semantic claim verification is implemented in Package 4, using the shared runtime.

## Gate

The lead verifies the source/edition mapping and license provenance, publication of a valid version covering at least 20 works, complete 200-record mapping, stable IDs and rebuild behavior, separate vectors for changed fingerprints, table limits, SQL ACL before planner/ranking, no-answer behavior, authorized citation reopening and exact spans. Tests exercise the PostgreSQL boundary on real isolated databases. The package also checks the common budget ledger and v1 regression. Only a passed gate permits Package 4; verified slices are committed separately following `commit.md`.
