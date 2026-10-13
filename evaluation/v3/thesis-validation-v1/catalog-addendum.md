# Catalog addendum for the independent candidate

The candidate roster uses products outside the local 200-product `eval` snapshot. The `app/v2` catalog reader is bound to `DatasetSource` rows in PostgreSQL, so a correct answer key alone cannot make those products available to the runtime. The current database source inventory is **NOT VERIFIED** while PostgreSQL is unavailable. Importing the complete `full` snapshot beside the `eval` snapshot would duplicate the old product IDs and make exact-title resolution ambiguous.

[`prepare_catalog_addendum.py`](prepare_catalog_addendum.py) creates a validated, metadata-only supplement. It contains all 1,577 products from the pinned full profile after excluding every product ID in the 200-product eval snapshot. It imports no review text. The 1,439 candidate catalog products and all 1,256 product IDs used by the 628 candidate tasks must be present in this supplement. The generated `provenance.json` binds the full snapshot, old eval snapshot, candidate manifest, preparation script, and output hashes.

## Prepare and verify locally

```powershell
.venv/Scripts/python.exe evaluation/v3/thesis-validation-v1/prepare_catalog_addendum.py --output output/evaluation-v3/thesis-validation-20261013/catalog-addendum-v2
.venv/Scripts/python.exe -m app.data.cli validate --snapshot output/evaluation-v3/thesis-validation-20261013/catalog-addendum-v2
```

The output path is one-shot; the script refuses to overwrite it. It validates the pinned source artifacts, including a local quality scan of the normalized review file, but writes no review rows and does not contact a model or PostgreSQL.

## DBA import

After PostgreSQL is available, first inspect the source inventory:

```sql
SELECT dataset_version, profile, product_count, products_sha256
FROM public.dataset_sources
WHERE dataset_id = 'tiki-books'
ORDER BY dataset_version, profile;
```

If a `full` profile or an existing source containing candidate product IDs is already imported, stop and reconcile the source inventory; do not import duplicate product rows. The current database inventory has not been observed. If the candidate IDs are absent, a DBA can import the prepared snapshot into the `evaluation` database using the existing transactional importer. Supply the DBA's `DATABASE_URL` through a secure process environment, not a command-line literal or checked-in file:

```powershell
.venv/Scripts/python.exe -m app.data.cli import --snapshot output/evaluation-v3/thesis-validation-20261013/catalog-addendum-v2
```

This adds a distinct `DatasetSource`, 1,577 `Product` rows, and zero `Review` rows. It does not widen `eval_runner` privileges. Importing changes the catalog snapshot identity, so create a fresh runtime manifest and bind the subsequent P7, P8, and operate run to that exact snapshot and source commit.

The independent catalog runner is not implemented yet. It should use the read-only merchant mode with `resolved_product_ids=()` and `ecommerce.read` plus `merchant.read` scopes, so each arm resolves titles from the catalog and does not seed demo offers for the entire 1,777-product catalog. Never inject answer-key product IDs as shopper context. A shopper-mode runner would need a sparse offer fixture for only the task's products. The addendum adds no knowledge documents, so this catalog cohort cannot support a document-recall or RAG contribution claim.
