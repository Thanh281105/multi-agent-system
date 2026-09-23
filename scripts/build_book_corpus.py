"""Build the reviewed books-v1 corpus through the shared provider ledger."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import create_database_engine
from app.knowledge.ingestion import CorpusIngestor
from app.knowledge.postgres import PostgresKnowledgeStore
from app.knowledge.v2_contracts import (
    BookMappingManifest,
    IndexBuildSpec,
    RetrievalPolicy,
    SourceManifest,
)
from app.shared.budget import (
    PricingManifest,
    ProviderBudgetContext,
    SQLProviderBudgetLedger,
    provider_budget_scope,
    usd_to_nano_usd,
)
from app.shared.embedding_runtime import OpenAIEmbeddingRuntime

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SOURCES = _ROOT / "data/knowledge/books-v1/sources.json"
_DEFAULT_MAPPINGS = _ROOT / "data/knowledge/books-v1/mappings.json"
_DEFAULT_PRICING = _ROOT / "app/shared/pricing-manifest.json"
_ContractT = TypeVar("_ContractT", bound=BaseModel)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build or resume the immutable books-v1 PostgreSQL corpus. The budget "
            "account and scope must already exist; this command never resets them."
        )
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--budget-account-id", required=True)
    parser.add_argument("--budget-scope-id", required=True)
    parser.add_argument(
        "--build-owner-id",
        help=(
            "Stable build owner used across safe resumes. Defaults to the first "
            "budget scope ID; pass the original value when resuming under a new scope."
        ),
    )
    parser.add_argument("--sources", type=Path, default=_DEFAULT_SOURCES)
    parser.add_argument("--mappings", type=Path, default=_DEFAULT_MAPPINGS)
    parser.add_argument(
        "--corpus-version",
        help=(
            "Override and revalidate both manifest corpus versions. Use this for "
            "a calibrated retrieval policy that requires a new human version."
        ),
    )
    parser.add_argument("--pricing-manifest", type=Path, default=_DEFAULT_PRICING)
    parser.add_argument(
        "--retrieval-policy",
        type=Path,
        help=(
            "Strict RetrievalPolicy JSON produced by development calibration. "
            "Changing it requires a new human corpus version."
        ),
    )
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--embedding-model", default="text-embedding-3-small")
    parser.add_argument("--embedding-dimension", type=int, default=1_536)
    parser.add_argument("--chunker-version", default="table_chunker_v1")
    parser.add_argument("--enrichment-policy-version", default="source_keywords_v1")
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--publish", action="store_true")
    return parser


def _retrieval_policy() -> RetrievalPolicy:
    return RetrievalPolicy(
        version="books_v1_rrf_v1",
        dense_weight=1.0,
        lexical_weight=1.0,
        rrf_k=60,
        candidate_limit=30,
        default_limit=6,
        max_limit=8,
        max_context_tokens=6_000,
        min_dense_relevance=0.2,
        min_lexical_coverage=0.1,
    )


def _read_manifest(path: Path, contract: type[_ContractT]) -> _ContractT:
    return contract.model_validate_json(path.read_text(encoding="utf-8"))


def _override_corpus_version(
    sources: SourceManifest,
    mappings: BookMappingManifest,
    corpus_version: str,
) -> tuple[SourceManifest, BookMappingManifest]:
    source_payload = sources.model_dump(mode="json")
    mapping_payload = mappings.model_dump(mode="json")
    source_payload["corpus_version"] = corpus_version
    mapping_payload["corpus_version"] = corpus_version
    return (
        SourceManifest.model_validate(source_payload),
        BookMappingManifest.model_validate(mapping_payload),
    )


def _validate_existing_budget(
    sessions: sessionmaker[Session],
    *,
    account_id: str,
    scope_id: str,
) -> None:
    """Validate the caller-created allocation without changing ledger state."""

    with sessions() as session:
        row = session.execute(
            text(
                "SELECT s.account_id, s.purpose, s.hard_limit_nano_usd "
                "FROM v2_budget_scopes AS s "
                "JOIN v2_budget_accounts AS a ON a.account_id = s.account_id "
                "WHERE s.scope_id = :scope_id AND a.account_id = :account_id"
            ),
            {"scope_id": scope_id, "account_id": account_id},
        ).one_or_none()
    if row is None:
        raise RuntimeError("existing ingestion budget account/scope was not found")
    if row.purpose != "ingestion":
        raise RuntimeError("budget scope purpose must be ingestion")
    if int(row.hard_limit_nano_usd) > usd_to_nano_usd("5"):
        raise RuntimeError("corpus budget scope must not exceed 5 USD")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sources = _read_manifest(args.sources, SourceManifest)
    mappings = _read_manifest(args.mappings, BookMappingManifest)
    if args.corpus_version is not None:
        sources, mappings = _override_corpus_version(
            sources,
            mappings,
            args.corpus_version,
        )
    retrieval_policy = (
        _read_manifest(args.retrieval_policy, RetrievalPolicy)
        if args.retrieval_policy is not None
        else _retrieval_policy()
    )
    pricing = PricingManifest.load(args.pricing_manifest)
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"API key environment variable is unset: {args.api_key_env}")

    engine = create_database_engine(args.database_url)
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )
    try:
        _validate_existing_budget(
            sessions,
            account_id=args.budget_account_id,
            scope_id=args.budget_scope_id,
        )
        ledger = SQLProviderBudgetLedger(sessions, pricing)
        budget = ProviderBudgetContext(
            ledger=ledger,
            scope_id=args.budget_scope_id,
            purpose="ingestion",
        )
        embedder = OpenAIEmbeddingRuntime(
            api_key,
            model=args.embedding_model,
            dimensions=args.embedding_dimension,
            max_retries=1,
        )
        ingestor = CorpusIngestor(
            store=PostgresKnowledgeStore(sessions),
            embedder=embedder,
            index_spec=IndexBuildSpec(
                embedding_model=args.embedding_model,
                embedding_dimension=args.embedding_dimension,
                chunker_version=args.chunker_version,
                enrichment_policy_version=args.enrichment_policy_version,
            ),
            retrieval_policy=retrieval_policy,
            embedding_batch_size=args.embedding_batch_size,
            build_owner_id=args.build_owner_id or args.budget_scope_id,
        )
        with provider_budget_scope(budget):
            result = ingestor.ingest(sources, mappings, publish=args.publish)
        print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
