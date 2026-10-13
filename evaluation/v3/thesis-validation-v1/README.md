# Thesis validation candidate pool v1

**Status: candidate-pool audit only. This is not a locked validation set and has not been sent to `app/v2`.**

The proposed blinded scoring guide is [human-rubric.md](human-rubric.md). No human ratings or reviewer commitments exist yet.

This artifact checks whether the pinned Tiki Books v4 archive has enough product groups disjoint from the old 200-product evaluation snapshot to plan a larger catalog validation. It does not generate prompts or gold labels, evaluate answers, or make a quality claim.

## Source and reproduction

The source is the [Kaggle Tiki Books v4 archive](https://www.kaggle.com/api/v1/datasets/download/biminhc/tiki-books-dataset?datasetVersionNumber=4) declared in the repository snapshot manifest (`CC0-1.0`), archive SHA-256 `371a618ed66425f8f2738ab01514ab2f20ed1bed645258b509fcce443bd32bbb`. The archive is not checked in. Download it, verify its hash, then prepare the normalized/redacted full profile and run the audit:

```powershell
.venv/Scripts/python.exe -m app.data.cli prepare --archive output/evaluation-v3/data-expansion-20261012/tiki-books-v4-source.zip --profile full --output output/evaluation-v3/data-expansion-20261012/tiki-books-v4-full
.venv/Scripts/python.exe evaluation/v3/thesis-validation-v1/audit_candidate_pool.py --output evaluation/v3/thesis-validation-v1/candidate-pool-audit.json
```

The audit verifies normalized product/review file hashes against the full-profile manifest. It removes every product external ID from `data/snapshots/tiki-books-v4-eval`, then also excludes any remaining record whose normalized title-author key matches a work group in that snapshot. It groups the remainder by normalized title and authors. The current audit found zero cross-snapshot title-author overlaps among ID-disjoint records. Normalization uses NFKD, case folding, diacritic removal, punctuation/whitespace folding, and removal of parenthetical phrases that contain a declared edition/format marker. This is still a screening heuristic; a human must merge duplicates across alternate titles/authors and reject unclear prompts before locking any task set.

## Current audit

The checked-in [candidate-pool-audit.json](candidate-pool-audit.json) records the output and all relevant data hashes, including prior work-group count and excluded duplicate-work records.

The pinned full profile contains 1,777 products and 76,755 cleaned/redacted reviews. After excluding the 200 old evaluation product IDs and checking the 198 old normalized title-author groups, the audit finds zero remaining cross-snapshot work-group overlaps, 1,577 candidate product records, and 1,553 heuristic title-author groups. The prompt-safe title/author and required price/rating filters used by the pair generator leave 1,439 title-author groups; page count is required only for the declared page-count stratum.

These counts make a 628-task, non-overlapping product-pair design numerically possible (1,256 distinct groups and 183 unused prompt-eligible groups) under the stated planning assumption `D=0.20`; [`paired_power.py`](paired_power.py) reproduces the conservative normal approximation. They do not prove 1,439 independent oracles: edition duplicates, task clarity, oracle accuracy, prompt leakage, and an independent audit remain unresolved. This pool is from the same Kaggle/Tiki source family, so it does not support a cross-source generalization claim. The catalog-only tasks do not establish a RAG/document-recall contribution; that needs a separately locked, new relevant-document cohort because the old 20-document knowledge corpus is development data.

The dataset includes review text. Only the normalized profile produced by the repository cleaner/redactor may be considered for future work; this audit reads the product file and checks the review-file digest without reading, printing, or sending review text. Do not submit the raw archive or raw comments to a model.
