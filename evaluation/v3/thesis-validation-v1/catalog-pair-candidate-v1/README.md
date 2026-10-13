# Catalog pair validation candidate v1

**Status: candidate only; not locked and not sent to `app/v2`.**

This generator created 628 deterministic two-book comparison prompts using
1256 disjoint title-author groups from the pinned Tiki Books v4 full
snapshot. All prompts ask for each book's snapshot price and rating and the cheaper
and higher-rated book. 100 predeclared tasks also ask for each page
count; the remaining tasks do not depend on page-count availability. The separate
answer key contains exact metadata values.

## Reproduction and hashes

Run `python evaluation/v3/thesis-validation-v1/build_catalog_pair_candidate.py`.
The manifest binds this generator, the normalized source snapshot, and all three
generated files. Hash-bound JSONL files use UTF-8 with LF line endings.
The source archive is CC0-1.0; raw review comments were not read or included.

## Required review before locking

- Human review must merge works missed by the title-author heuristic and reject
  prompts where a title/author could resolve ambiguously.
- Verify oracle values against the exact catalog snapshot and its record IDs.
- Review `author-overlap-audit.json` and pre-register how repeated authors affect
  confidence intervals and power. Its connected components are a dependence
  screen, not a replacement sample size.
- Build a dynamic runner that calls the same `app/v2` runtime and logs catalog
  candidates/citations; this suite does not test RAG document recall.
- Freeze the judge, blinding, analysis, budget, SUT commit, and schedule before
  sending any prompt to the model.

`answer-key.jsonl` is separate from `prompts.jsonl`; do not pass it to the SUT.
