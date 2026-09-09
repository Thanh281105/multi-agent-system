"""Collect one budgeted query-embedding cache and score policies offline.

This Package 3 development runner never creates a budget account/scope, changes a
published corpus, or retries a pending/unknown cache. Collection claims the cache path
exclusively before its single ``embed_many`` dispatch. Evaluation replaces only the
retrieval policy in an in-memory snapshot wrapper and performs no provider calls.

Examples (PowerShell)::

    python scripts/calibrate_book_retrieval.py collect `
      --database-url $env:DATABASE_URL `
      --corpus-version-id cor_... --index-manifest-id idx_... `
      --tenant-id tenant_admin --principal-id calibration_admin --mode shopper `
      --access-scope ecommerce.read --budget-account-id shared `
      --budget-scope-id p3_calibration --api-key-env OPENAI_API_KEY `
      --cache .ai-router/logs/p3-calibration-cache.json

    python scripts/calibrate_book_retrieval.py evaluate `
      --database-url $env:DATABASE_URL `
      --corpus-version-id cor_... --index-manifest-id idx_... `
      --tenant-id tenant_admin --principal-id calibration_admin --mode shopper `
      --access-scope ecommerce.read --cache .ai-router/logs/p3-calibration-cache.json `
      --output .ai-router/reports/p3-calibration-results.json

A pending/unknown cache is terminal evidence of an uncertain dispatch. Reconcile it
with the ledger; never delete it merely to retry the same scope/call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, TypeVar
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import create_database_engine
from app.knowledge.postgres import PostgresKnowledgeStore
from app.knowledge.retrieval import (
    DeterministicKnowledgeQueryPlanner,
    HybridKnowledgeRetriever,
    KnowledgeQueryPlan,
    KnowledgeRetrievalStore,
)
from app.knowledge.v2_contracts import (
    AuthorizedKnowledgeSource,
    PublishedKnowledgeSnapshot,
    ResolvedKnowledgeEvidence,
    RetrievalChunk,
    RetrievalPolicy,
    canonical_json_sha256,
)
from app.shared.budget import (
    BudgetLedger,
    PricingManifest,
    ProviderBudgetContext,
    ScopeUsageSummary,
    SQLProviderBudgetLedger,
    provider_budget_scope,
    usd_to_nano_usd,
)
from app.shared.embedding_runtime import EmbeddingRuntime, OpenAIEmbeddingRuntime
from app.v2.authorization import (
    DEMO_STORE_ID,
    ResourceAuthorization,
    ResourceBinding,
    required_scopes_for_mode,
)
from app.v2.contracts import ConversationMode, V2Contract

_ROOT = Path(__file__).resolve().parents[1]
_PROBES_PATH = _ROOT / "data/knowledge/books-v1/development-probes.json"
_PRICING_PATH = _ROOT / "app/shared/pricing-manifest.json"
_FIXED_PROBE_MANIFEST_SHA256 = (
    "c1fd1801dc2481384ca8383f21eb140c2ee36238906c7ea294c1d544bfce3433"
)
_FIXED_PRE_SUT_PLAN_SHA256 = (
    "8f365b18451486245242e94b5d125e14c7b24713ff4a48ca7f3b2c2007cfe4a4"
)
_CACHE_SCHEMA_VERSION: Final = "1.0"
_OUTPUT_SCHEMA_VERSION: Final = "1.0"
_MAX_SCOPE_USD = "5"
_ContractT = TypeVar("_ContractT", bound=V2Contract)


class CalibrationError(RuntimeError):
    """The calibration inputs or state cannot produce trustworthy output."""


class DevelopmentProbe(V2Contract):
    probe_id: str = Field(pattern=r"^p3-dev-[0-9]{2}$")
    query: str = Field(min_length=1, max_length=500)
    expected_source_ids: tuple[str, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def validate_sources(self) -> DevelopmentProbe:
        if len(self.expected_source_ids) != len(set(self.expected_source_ids)):
            raise ValueError("expected source IDs must be unique")
        return self


class DevelopmentProbeManifest(V2Contract):
    schema_version: Literal["1.0"]
    protocol_id: Literal["p3-development-retrieval-v1"]
    purpose: Literal["development_retrieval_relevance_only"]
    pre_sut_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    held_out_excluded_product_ids: tuple[int, ...] = Field(min_length=4, max_length=4)
    held_out_excluded_work_ids: tuple[str, ...] = Field(min_length=4, max_length=4)
    held_out_excluded_negative_families: tuple[str, ...] = Field(
        min_length=4, max_length=4
    )
    probes: tuple[DevelopmentProbe, ...] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def validate_protocol(self) -> DevelopmentProbeManifest:
        if self.pre_sut_plan_sha256 != _FIXED_PRE_SUT_PLAN_SHA256:
            raise ValueError("development probe pre-SUT identity changed")
        probe_ids = [probe.probe_id for probe in self.probes]
        if len(probe_ids) != len(set(probe_ids)):
            raise ValueError("development probe IDs must be unique")
        positive = sum(bool(probe.expected_source_ids) for probe in self.probes)
        if positive != 4:
            raise ValueError(
                "development probes require four positive and four negative"
            )
        exclusions: Sequence[Sequence[object]] = (
            self.held_out_excluded_product_ids,
            self.held_out_excluded_work_ids,
            self.held_out_excluded_negative_families,
        )
        if any(len(values) != len(set(values)) for values in exclusions):
            raise ValueError("held-out exclusions must be unique")
        return self


class PlannedProbe(V2Contract):
    probe_id: str
    query: str
    expected_source_ids: tuple[str, ...]
    planned_queries: tuple[str, ...] = Field(min_length=1, max_length=4)
    planned_source_ids: tuple[str, ...]


class CalibrationManifest(V2Contract):
    schema_version: Literal["1.0"] = _CACHE_SCHEMA_VERSION
    protocol_id: str
    probe_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pre_sut_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_mode: ConversationMode
    access_scopes: tuple[str, ...]
    authorized_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_version_id: str
    index_manifest_id: str
    index_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_embedding_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_model: str
    embedding_dimension: int = Field(ge=32, le=4_096)
    stored_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planned_probes: tuple[PlannedProbe, ...]
    planned_queries_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unique_queries: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_planned_queries(self) -> CalibrationManifest:
        expected = tuple(
            dict.fromkeys(
                query
                for probe in self.planned_probes
                for query in probe.planned_queries
            )
        )
        if self.unique_queries != expected:
            raise ValueError("unique planned queries are not first-seen deduplicated")
        expected_sha = _planned_queries_sha256(self.planned_probes, self.unique_queries)
        if self.planned_queries_sha256 != expected_sha:
            raise ValueError("planned query fingerprint is invalid")
        return self

    @property
    def fingerprint(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class CollectionClaim(V2Contract):
    manifest: CalibrationManifest
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_account_id: str = Field(min_length=1, max_length=120)
    budget_scope_id: str = Field(min_length=1, max_length=160)
    provider_call_id: str = Field(min_length=1, max_length=96)

    @model_validator(mode="after")
    def validate_identity(self) -> CollectionClaim:
        if self.manifest_sha256 != self.manifest.fingerprint:
            raise ValueError("calibration manifest fingerprint is invalid")
        expected_call_id = _stable_provider_call_id(
            self.manifest_sha256, self.budget_scope_id
        )
        if self.provider_call_id != expected_call_id:
            raise ValueError("provider call ID is not stable for this calibration")
        return self


class UsageSnapshot(V2Contract):
    known_nano_usd: int = Field(ge=0)
    reserved_nano_usd: int = Field(ge=0)
    unknown_nano_usd: int = Field(ge=0)
    hard_limit_nano_usd: int = Field(gt=0)
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    provider_attempts: int = Field(ge=0)
    pending_attempts: int = Field(ge=0)
    unknown_cost_attempts: int = Field(ge=0)
    attempts_without_usage: int = Field(ge=0)


class CachedQueryVector(V2Contract):
    query: str = Field(min_length=1, max_length=500)
    vector: tuple[float, ...] = Field(min_length=32, max_length=4_096)

    @model_validator(mode="after")
    def validate_finite(self) -> CachedQueryVector:
        if any(not math.isfinite(value) for value in self.vector):
            raise ValueError("cached query vector contains a non-finite value")
        return self


CacheStatus = Literal["pending", "complete", "unknown"]


class CalibrationCache(V2Contract):
    schema_version: Literal["1.0"] = _CACHE_SCHEMA_VERSION
    status: CacheStatus
    claim: CollectionClaim
    claimed_at: AwareDatetime
    usage_before: UsageSnapshot
    usage_after: UsageSnapshot | None = None
    vectors: tuple[CachedQueryVector, ...] = ()
    vector_payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure: str | None = None

    @model_validator(mode="after")
    def validate_state(self) -> CalibrationCache:
        if self.status == "pending":
            if (
                self.usage_after is not None
                or self.vectors
                or self.vector_payload_sha256 is not None
                or self.failure is not None
            ):
                raise ValueError("pending cache contains completed state")
        elif self.status == "complete":
            if (
                self.usage_after is None
                or not self.vectors
                or self.vector_payload_sha256 is None
                or self.failure is not None
            ):
                raise ValueError("complete cache is incomplete")
            if self.vector_payload_sha256 != _vector_payload_sha256(self.vectors):
                raise ValueError("complete cache vector payload digest is invalid")
        elif (
            self.usage_after is None
            or self.failure is None
            or self.vectors
            or self.vector_payload_sha256 is not None
        ):
            raise ValueError(
                "unknown cache requires usage and no completed vector payload"
            )
        return self


@dataclass(frozen=True, slots=True)
class PreparedCalibration:
    manifest: CalibrationManifest
    snapshot: PublishedKnowledgeSnapshot


@dataclass(frozen=True, slots=True)
class CollectionResult:
    cache: CalibrationCache
    reused: bool


class CachedQueryEmbedder:
    """Exact query-vector lookup; any cache miss fails instead of dispatching."""

    def __init__(self, cache: CalibrationCache) -> None:
        _validate_complete_cache(cache, cache.claim)
        self.model = cache.claim.manifest.embedding_model
        self.dimensions = cache.claim.manifest.embedding_dimension
        self.method = f"cached_{cache.claim.manifest.query_embedding_fingerprint}"
        self._vectors = {item.query: item.vector for item in cache.vectors}

    def embed(self, text_value: str) -> list[float]:
        return self.embed_many([text_value])[0]

    def embed_many(self, text_values: list[str]) -> list[list[float]]:
        try:
            return [list(self._vectors[value.strip()]) for value in text_values]
        except KeyError as exc:
            raise CalibrationError(
                "planned query is absent from complete cache"
            ) from exc


class PolicyOverrideStore:
    """Read-only delegate returning one policy-only counterfactual snapshot."""

    def __init__(
        self,
        delegate: KnowledgeRetrievalStore,
        stored_snapshot: PublishedKnowledgeSnapshot,
        candidate_policy: RetrievalPolicy,
    ) -> None:
        self._delegate = delegate
        self._stored_snapshot = stored_snapshot
        self._counterfactual = PublishedKnowledgeSnapshot.model_validate(
            {
                **stored_snapshot.model_dump(mode="json"),
                "retrieval_policy": candidate_policy.model_dump(mode="json"),
            }
        )

    def resolve_published_snapshot(
        self,
        corpus_version_id: str,
        *,
        index_manifest_id: str | None = None,
    ) -> PublishedKnowledgeSnapshot:
        if (
            corpus_version_id != self._stored_snapshot.corpus_version_id
            or index_manifest_id != self._stored_snapshot.index_manifest_id
        ):
            raise CalibrationError("counterfactual snapshot identity changed")
        return self._counterfactual

    def list_authorized_sources(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        product_ids: tuple[int, ...] = (),
    ) -> tuple[AuthorizedKnowledgeSource, ...]:
        return self._delegate.list_authorized_sources(
            snapshot, access, product_ids=product_ids
        )

    def load_authorized_chunks(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        source_ids: frozenset[str],
    ) -> tuple[RetrievalChunk, ...]:
        return self._delegate.load_authorized_chunks(
            snapshot, access, source_ids=source_ids
        )

    def resolve_authorized_evidence(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        source_id: str,
        source_version_id: str,
        chunk_id: str,
        span_id: str,
    ) -> ResolvedKnowledgeEvidence:
        return self._delegate.resolve_authorized_evidence(
            snapshot,
            access,
            source_id=source_id,
            source_version_id=source_version_id,
            chunk_id=chunk_id,
            span_id=span_id,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect Package 3 development query embeddings once, then compare "
            "retrieval policies offline. This does not publish a policy."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Cache recovery: complete+matching is reusable; pending, unknown, "
            "malformed, or mismatched caches fail closed and are never overwritten."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser(
        "collect", help="Claim a cache and make one budgeted embedding batch."
    )
    evaluate = commands.add_parser(
        "evaluate", help="Score policy candidates from a complete cache offline."
    )
    for command in (collect, evaluate):
        _add_snapshot_arguments(command)
        command.add_argument("--cache", type=Path, required=True)
    collect.add_argument("--budget-account-id", required=True)
    collect.add_argument("--budget-scope-id", required=True)
    collect.add_argument("--pricing-manifest", type=Path, default=_PRICING_PATH)
    collect.add_argument("--api-key-env", default="OPENAI_API_KEY")
    evaluate.add_argument(
        "--policy",
        action="append",
        type=Path,
        default=[],
        help=(
            "Full RetrievalPolicy JSON; repeat for multiple candidates. "
            "With none supplied, a fixed six-policy threshold grid is used."
        ),
    )
    evaluate.add_argument("--output", type=Path, required=True)
    return parser


def _add_snapshot_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--corpus-version-id", required=True)
    parser.add_argument("--index-manifest-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--principal-id", required=True)
    parser.add_argument(
        "--mode", required=True, choices=tuple(mode.value for mode in ConversationMode)
    )
    parser.add_argument(
        "--access-scope",
        action="append",
        required=True,
        help="Current server-fixture scope; repeat for each exact scope.",
    )


def read_fixed_probe_manifest() -> tuple[DevelopmentProbeManifest, str]:
    try:
        raw = _PROBES_PATH.read_bytes()
        probes = DevelopmentProbeManifest.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise CalibrationError("fixed development probe manifest is invalid") from exc
    sha256 = canonical_json_sha256(probes.model_dump(mode="json"))
    if sha256 != _FIXED_PROBE_MANIFEST_SHA256:
        raise CalibrationError("fixed development probe manifest fingerprint changed")
    return probes, sha256


def prepare_calibration(
    store: KnowledgeRetrievalStore,
    probes: DevelopmentProbeManifest,
    probe_manifest_sha256: str,
    access: ResourceAuthorization,
    *,
    corpus_version_id: str,
    index_manifest_id: str,
) -> PreparedCalibration:
    snapshot = store.resolve_published_snapshot(
        corpus_version_id, index_manifest_id=index_manifest_id
    )
    if (
        snapshot.corpus_version_id != corpus_version_id
        or snapshot.index_manifest_id != index_manifest_id
    ):
        raise CalibrationError("published snapshot identity is not explicit")
    sources = store.list_authorized_sources(snapshot, access)
    if not sources:
        raise CalibrationError("authorized calibration catalog is empty")
    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise CalibrationError("authorized calibration sources are duplicated")
    plans = asyncio.run(_plan_probes(probes.probes, sources))
    planned_probes = tuple(
        PlannedProbe(
            probe_id=probe.probe_id,
            query=probe.query,
            expected_source_ids=probe.expected_source_ids,
            planned_queries=plan.queries,
            planned_source_ids=plan.source_ids,
        )
        for probe, plan in zip(probes.probes, plans, strict=True)
    )
    unique_queries = tuple(
        dict.fromkeys(
            query for plan in planned_probes for query in plan.planned_queries
        )
    )
    planned_queries_sha256 = _planned_queries_sha256(planned_probes, unique_queries)
    manifest = CalibrationManifest(
        protocol_id=probes.protocol_id,
        probe_manifest_sha256=probe_manifest_sha256,
        pre_sut_plan_sha256=probes.pre_sut_plan_sha256,
        access_fingerprint=canonical_json_sha256(access.model_dump(mode="json")),
        access_mode=access.binding.mode,
        access_scopes=tuple(sorted(access.scopes)),
        authorized_catalog_sha256=canonical_json_sha256(
            [source.model_dump(mode="json") for source in sources]
        ),
        corpus_version_id=snapshot.corpus_version_id,
        index_manifest_id=snapshot.index_manifest_id,
        index_fingerprint=snapshot.index_fingerprint,
        query_embedding_fingerprint=snapshot.query_embedding_fingerprint,
        embedding_model=snapshot.embedding_model,
        embedding_dimension=snapshot.embedding_dimension,
        stored_policy_sha256=canonical_json_sha256(
            snapshot.retrieval_policy.model_dump(mode="json")
        ),
        planned_probes=planned_probes,
        planned_queries_sha256=planned_queries_sha256,
        unique_queries=unique_queries,
    )
    return PreparedCalibration(manifest=manifest, snapshot=snapshot)


async def _plan_probes(
    probes: Sequence[DevelopmentProbe],
    sources: Sequence[AuthorizedKnowledgeSource],
) -> tuple[KnowledgeQueryPlan, ...]:
    planner = DeterministicKnowledgeQueryPlanner()
    return tuple([await planner.plan(probe.query, sources) for probe in probes])


def collect_query_embeddings(
    manifest: CalibrationManifest,
    cache_path: Path,
    *,
    budget_account_id: str,
    budget_scope_id: str,
    ledger: BudgetLedger,
    embedder_factory: Callable[[], EmbeddingRuntime],
) -> CollectionResult:
    claim = _collection_claim(
        manifest,
        budget_account_id=budget_account_id,
        budget_scope_id=budget_scope_id,
    )
    if cache_path.exists():
        cache = _read_cache(cache_path)
        _validate_complete_cache(cache, claim)
        return CollectionResult(cache=cache, reused=True)

    usage_before = _usage_snapshot(ledger.scope_usage_summary(budget_scope_id))
    if (
        usage_before.pending_attempts
        or usage_before.unknown_cost_attempts
        or usage_before.attempts_without_usage
    ):
        raise CalibrationError("budget scope has pending or unknown usage")
    embedder = embedder_factory()
    if (
        getattr(embedder, "model", None) != manifest.embedding_model
        or embedder.dimensions != manifest.embedding_dimension
    ):
        raise CalibrationError("collector embedder does not match published snapshot")
    pending = CalibrationCache(
        status="pending",
        claim=claim,
        claimed_at=datetime.now(UTC),
        usage_before=usage_before,
    )
    try:
        _write_exclusive_json(cache_path, pending.model_dump(mode="json"))
    except FileExistsError:
        cache = _read_cache(cache_path)
        _validate_complete_cache(cache, claim)
        return CollectionResult(cache=cache, reused=True)

    budget = ProviderBudgetContext(
        ledger=ledger,
        scope_id=budget_scope_id,
        purpose="ingestion",
        call_id_factory=lambda operation: _claim_call_id(claim, operation),
        max_retries=1,
    )
    with provider_budget_scope(budget):
        raw_vectors = embedder.embed_many(list(manifest.unique_queries))
    vectors = _validated_query_vectors(manifest, raw_vectors)
    usage_after = _usage_snapshot(ledger.scope_usage_summary(budget_scope_id))
    _usage_delta(usage_before, usage_after)
    complete = CalibrationCache(
        status="complete",
        claim=claim,
        claimed_at=pending.claimed_at,
        usage_before=usage_before,
        usage_after=usage_after,
        vectors=vectors,
        vector_payload_sha256=_vector_payload_sha256(vectors),
    )
    _replace_pending_cache(cache_path, pending, complete)
    _validate_complete_cache(complete, claim)
    return CollectionResult(cache=complete, reused=False)


def evaluate_policy_candidates(
    store: KnowledgeRetrievalStore,
    prepared: PreparedCalibration,
    probes: DevelopmentProbeManifest,
    access: ResourceAuthorization,
    cache: CalibrationCache,
    policies: Sequence[RetrievalPolicy],
) -> dict[str, Any]:
    _validate_evaluation_cache(cache, prepared.manifest)
    if not policies:
        raise CalibrationError("at least one candidate policy is required")
    versions = [policy.version for policy in policies]
    if len(versions) != len(set(versions)):
        raise CalibrationError("candidate policy versions must be unique")
    cached_embedder = CachedQueryEmbedder(cache)
    candidates = [
        asyncio.run(
            _evaluate_policy(
                store,
                prepared.snapshot,
                access,
                probes,
                policy,
                cached_embedder,
            )
        )
        for policy in policies
    ]
    separating = [
        candidate["candidate_policy"]["version"]
        for candidate in candidates
        if candidate["metrics"]["separating_threshold"]
    ]
    assert cache.usage_after is not None
    return {
        "schema_version": _OUTPUT_SCHEMA_VERSION,
        "protocol_id": probes.protocol_id,
        "counterfactual_only": True,
        "published_database_mutated": False,
        "calibration_manifest_sha256": prepared.manifest.fingerprint,
        "probe_manifest_sha256": prepared.manifest.probe_manifest_sha256,
        "query_embedding_fingerprint": prepared.manifest.query_embedding_fingerprint,
        "collection_snapshot": {
            "manifest_sha256": cache.claim.manifest_sha256,
            "corpus_version_id": cache.claim.manifest.corpus_version_id,
            "index_manifest_id": cache.claim.manifest.index_manifest_id,
            "index_fingerprint": cache.claim.manifest.index_fingerprint,
            "authorized_catalog_sha256": (
                cache.claim.manifest.authorized_catalog_sha256
            ),
            "access_fingerprint": cache.claim.manifest.access_fingerprint,
            "stored_policy_sha256": cache.claim.manifest.stored_policy_sha256,
        },
        "evaluation_snapshot": {
            "corpus_version_id": prepared.snapshot.corpus_version_id,
            "index_manifest_id": prepared.snapshot.index_manifest_id,
            "index_fingerprint": prepared.snapshot.index_fingerprint,
            "authorized_catalog_sha256": (prepared.manifest.authorized_catalog_sha256),
            "access_fingerprint": prepared.manifest.access_fingerprint,
            "stored_policy": prepared.snapshot.retrieval_policy.model_dump(mode="json"),
            "stored_policy_sha256": prepared.manifest.stored_policy_sha256,
        },
        "collection": {
            "budget_scope_id": cache.claim.budget_scope_id,
            "provider_call_id": cache.claim.provider_call_id,
            "vector_payload_sha256": cache.vector_payload_sha256,
            "cache_sha256": canonical_json_sha256(cache.model_dump(mode="json")),
            "usage_before": cache.usage_before.model_dump(mode="json"),
            "usage_after": cache.usage_after.model_dump(mode="json"),
            "usage_delta": _usage_delta(
                cache.usage_before, cache.usage_after
            ).model_dump(mode="json"),
            "usage_fully_known": _collection_usage_is_known(
                _usage_delta(cache.usage_before, cache.usage_after)
            ),
        },
        "candidates": candidates,
        "separating_threshold_found": bool(separating),
        "separating_candidate_policy_versions": separating,
        "claim_limitations": (
            "Development relevance only; no semantic entailment, held-out benchmark, "
            "or publication claim."
        ),
    }


async def _evaluate_policy(
    store: KnowledgeRetrievalStore,
    stored_snapshot: PublishedKnowledgeSnapshot,
    access: ResourceAuthorization,
    probes: DevelopmentProbeManifest,
    policy: RetrievalPolicy,
    embedder: CachedQueryEmbedder,
) -> dict[str, Any]:
    wrapper = PolicyOverrideStore(store, stored_snapshot, policy)
    retriever = HybridKnowledgeRetriever(
        wrapper,
        embedder,
        planner=DeterministicKnowledgeQueryPlanner(),
    )
    probe_results: list[dict[str, Any]] = []
    for probe in probes.probes:
        bundle = await retriever.retrieve(
            probe.query,
            access,
            corpus_version_id=stored_snapshot.corpus_version_id,
            index_manifest_id=stored_snapshot.index_manifest_id,
        )
        selected_source_ids = tuple(
            dict.fromkeys(hit.source.source_id for hit in bundle.hits)
        )
        expected = set(probe.expected_source_ids)
        selected = set(selected_source_ids)
        positive = bool(expected)
        diagnostics = []
        for diagnostic in bundle.diagnostics:
            row = asdict(diagnostic)
            row["channels"] = [
                channel
                for channel, included in (
                    ("bm25", diagnostic.lexical_candidate),
                    ("dense", diagnostic.dense_candidate),
                )
                if included
            ]
            diagnostics.append(row)
        probe_results.append(
            {
                "probe_id": probe.probe_id,
                "query": probe.query,
                "expected_source_ids": list(probe.expected_source_ids),
                "planned_queries": list(bundle.plan.queries),
                "selected_source_ids": list(selected_source_ids),
                "selected_chunk_ids": [hit.chunk.chunk_id for hit in bundle.hits],
                "answerable": bundle.answerable,
                "context_token_count": bundle.context_token_count,
                "max_context_tokens": policy.max_context_tokens,
                "within_context_bound": (
                    bundle.context_token_count <= policy.max_context_tokens
                ),
                "expected_match": {
                    "probe_kind": "positive" if positive else "negative",
                    "expected_source_recall": (
                        len(expected & selected) / len(expected) if positive else None
                    ),
                    "all_expected_sources_selected": (
                        expected <= selected if positive else None
                    ),
                    "exact_expected_source_set": selected == expected,
                    "unexpected_selected_source_count": len(selected - expected),
                    "correct_no_answer": (
                        (not bundle.answerable) if not positive else None
                    ),
                },
                "diagnostics": diagnostics,
            }
        )
    positive_results = [
        result
        for result in probe_results
        if result["expected_match"]["probe_kind"] == "positive"
    ]
    negative_results = [
        result
        for result in probe_results
        if result["expected_match"]["probe_kind"] == "negative"
    ]
    positive_full = sum(
        result["expected_match"]["all_expected_sources_selected"] is True
        for result in positive_results
    )
    negative_correct = sum(
        result["expected_match"]["correct_no_answer"] is True
        for result in negative_results
    )
    return {
        "counterfactual_only": True,
        "candidate_policy": policy.model_dump(mode="json"),
        "candidate_policy_sha256": canonical_json_sha256(
            policy.model_dump(mode="json")
        ),
        "metrics": {
            "positive_probe_count": len(positive_results),
            "positive_all_expected_count": positive_full,
            "negative_probe_count": len(negative_results),
            "negative_correct_no_answer_count": negative_correct,
            "separating_threshold": (
                positive_full == len(positive_results)
                and negative_correct == len(negative_results)
            ),
        },
        "probes": probe_results,
    }


def default_candidate_grid(stored: RetrievalPolicy) -> tuple[RetrievalPolicy, ...]:
    return tuple(
        RetrievalPolicy.model_validate(
            {
                **stored.model_dump(mode="json"),
                "version": f"p3_cal_d{dense:03d}_l{lexical:03d}",
                "min_dense_relevance": dense / 100,
                "min_lexical_coverage": lexical / 100,
            }
        )
        for dense in (15, 20, 25)
        for lexical in (5, 10)
    )


def _validated_query_vectors(
    manifest: CalibrationManifest,
    raw_vectors: Sequence[Sequence[float]],
) -> tuple[CachedQueryVector, ...]:
    if len(raw_vectors) != len(manifest.unique_queries):
        raise CalibrationError("embedding cache cardinality mismatch")
    if any(len(vector) != manifest.embedding_dimension for vector in raw_vectors):
        raise CalibrationError("embedding cache vector dimension mismatch")
    try:
        return tuple(
            CachedQueryVector(
                query=query, vector=tuple(float(value) for value in vector)
            )
            for query, vector in zip(manifest.unique_queries, raw_vectors, strict=True)
        )
    except ValueError as exc:
        raise CalibrationError("embedding cache vector is invalid") from exc


def _validate_complete_cache(
    cache: CalibrationCache,
    expected_claim: CollectionClaim,
) -> None:
    if cache.claim != expected_claim:
        raise CalibrationError("calibration cache identity mismatch")
    if cache.status != "complete":
        raise CalibrationError(
            f"calibration cache is {cache.status}; automatic redispatch is forbidden"
        )
    queries = tuple(item.query for item in cache.vectors)
    if queries != cache.claim.manifest.unique_queries:
        raise CalibrationError("calibration cache queries are incomplete or reordered")
    dimension = cache.claim.manifest.embedding_dimension
    if any(len(item.vector) != dimension for item in cache.vectors):
        raise CalibrationError("calibration cache vector dimension mismatch")
    if cache.usage_after is None:
        raise CalibrationError("complete calibration cache has no usage summary")
    _usage_delta(cache.usage_before, cache.usage_after)


def _vector_payload_sha256(vectors: Sequence[CachedQueryVector]) -> str:
    return canonical_json_sha256([vector.model_dump(mode="json") for vector in vectors])


def _validate_evaluation_cache(
    cache: CalibrationCache,
    evaluation_manifest: CalibrationManifest,
) -> None:
    _validate_complete_cache(cache, cache.claim)
    collection = cache.claim.manifest
    vector_identity = (
        "protocol_id",
        "probe_manifest_sha256",
        "pre_sut_plan_sha256",
        "planned_queries_sha256",
        "unique_queries",
        "query_embedding_fingerprint",
        "embedding_model",
        "embedding_dimension",
    )
    if any(
        getattr(collection, field) != getattr(evaluation_manifest, field)
        for field in vector_identity
    ):
        raise CalibrationError(
            "calibration cache probe/query/model identity does not match evaluation"
        )


def _planned_queries_sha256(
    planned_probes: Sequence[PlannedProbe],
    unique_queries: Sequence[str],
) -> str:
    return canonical_json_sha256(
        {
            "planned_queries": [
                {
                    "probe_id": probe.probe_id,
                    "planned_queries": list(probe.planned_queries),
                }
                for probe in planned_probes
            ],
            "unique_queries": list(unique_queries),
        }
    )


def _collection_claim(
    manifest: CalibrationManifest,
    *,
    budget_account_id: str,
    budget_scope_id: str,
) -> CollectionClaim:
    manifest_sha256 = manifest.fingerprint
    return CollectionClaim(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        budget_account_id=budget_account_id,
        budget_scope_id=budget_scope_id,
        provider_call_id=_stable_provider_call_id(manifest_sha256, budget_scope_id),
    )


def _stable_provider_call_id(manifest_sha256: str, scope_id: str) -> str:
    suffix = canonical_json_sha256(
        {"manifest_sha256": manifest_sha256, "budget_scope_id": scope_id}
    )[:48]
    return f"ecall_cal_{suffix}"


def _claim_call_id(claim: CollectionClaim, operation: str) -> str:
    if operation != "embedding":
        raise CalibrationError("calibration cache permits only embedding dispatch")
    return claim.provider_call_id


def _usage_snapshot(summary: ScopeUsageSummary) -> UsageSnapshot:
    return UsageSnapshot(
        known_nano_usd=summary.costs.known_nano_usd,
        reserved_nano_usd=summary.costs.reserved_nano_usd,
        unknown_nano_usd=summary.costs.unknown_nano_usd,
        hard_limit_nano_usd=summary.costs.hard_limit_nano_usd,
        input_tokens=summary.input_tokens,
        cached_input_tokens=summary.cached_input_tokens,
        output_tokens=summary.output_tokens,
        reasoning_tokens=summary.reasoning_tokens,
        total_tokens=summary.total_tokens,
        provider_attempts=summary.provider_attempts,
        pending_attempts=summary.pending_attempts,
        unknown_cost_attempts=summary.unknown_cost_attempts,
        attempts_without_usage=summary.attempts_without_usage,
    )


def _usage_delta(before: UsageSnapshot, after: UsageSnapshot) -> UsageSnapshot:
    values = {
        name: getattr(after, name) - getattr(before, name)
        for name in UsageSnapshot.model_fields
        if name != "hard_limit_nano_usd"
    }
    if any(value < 0 for value in values.values()):
        raise CalibrationError("budget usage moved backwards during collection")
    return UsageSnapshot(
        **values,
        hard_limit_nano_usd=after.hard_limit_nano_usd,
    )


def _collection_usage_is_known(delta: UsageSnapshot) -> bool:
    return (
        delta.provider_attempts in (1, 2)
        and delta.pending_attempts == 0
        and delta.unknown_cost_attempts == 0
        and delta.attempts_without_usage == 0
        and delta.reserved_nano_usd == 0
        and delta.unknown_nano_usd == 0
    )


def _read_cache(path: Path) -> CalibrationCache:
    try:
        return CalibrationCache.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CalibrationError("calibration cache is malformed or unreadable") from exc


def _replace_pending_cache(
    path: Path,
    expected: CalibrationCache,
    replacement: CalibrationCache,
) -> None:
    current = _read_cache(path)
    if current != expected or current.status != "pending":
        raise CalibrationError("pending calibration cache claim changed")
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _write_exclusive_json(temp, replacement.model_dump(mode="json"))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _write_exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    if not path.parent.is_dir():
        raise CalibrationError("output parent directory does not exist")
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _access_fixture(args: argparse.Namespace) -> ResourceAuthorization:
    scopes = tuple(args.access_scope)
    if len(scopes) != len(set(scopes)) or any(not scope.strip() for scope in scopes):
        raise CalibrationError(
            "server-fixture access scopes must be unique and nonblank"
        )
    mode = ConversationMode(args.mode)
    access = ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id=args.tenant_id,
            principal_id=args.principal_id,
            mode=mode,
            store_id=DEMO_STORE_ID,
        ),
        scopes=frozenset(scopes),
    )
    if not required_scopes_for_mode(mode) <= access.scopes:
        raise CalibrationError(
            "server-fixture scopes do not authorize the selected mode"
        )
    return access


def _validate_existing_budget(
    sessions: sessionmaker[Session],
    *,
    account_id: str,
    scope_id: str,
) -> None:
    with sessions() as session:
        row = session.execute(
            text(
                "SELECT scopes.account_id, scopes.purpose, "
                "scopes.hard_limit_nano_usd "
                "FROM v2_budget_scopes AS scopes "
                "JOIN v2_budget_accounts AS accounts "
                "ON accounts.account_id = scopes.account_id "
                "WHERE scopes.scope_id = :scope_id "
                "AND accounts.account_id = :account_id"
            ),
            {"scope_id": scope_id, "account_id": account_id},
        ).one_or_none()
    if row is None:
        raise CalibrationError(
            "existing calibration budget account/scope was not found"
        )
    if row.purpose != "ingestion":
        raise CalibrationError("calibration budget scope purpose must be ingestion")
    if int(row.hard_limit_nano_usd) > usd_to_nano_usd(_MAX_SCOPE_USD):
        raise CalibrationError("calibration budget scope must not exceed 5 USD")


def _read_policy(path: Path) -> RetrievalPolicy:
    try:
        return RetrievalPolicy.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CalibrationError(f"candidate policy is invalid: {path}") from exc


def _session_factory(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_database_engine(database_url)
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )
    return engine, sessions


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    probes, probe_sha256 = read_fixed_probe_manifest()
    access = _access_fixture(args)
    engine, sessions = _session_factory(args.database_url)
    try:
        store = PostgresKnowledgeStore(sessions)
        prepared = prepare_calibration(
            store,
            probes,
            probe_sha256,
            access,
            corpus_version_id=args.corpus_version_id,
            index_manifest_id=args.index_manifest_id,
        )
        if args.command == "collect":
            _validate_existing_budget(
                sessions,
                account_id=args.budget_account_id,
                scope_id=args.budget_scope_id,
            )
            pricing = PricingManifest.load(args.pricing_manifest)
            ledger = SQLProviderBudgetLedger(sessions, pricing)

            def embedder_factory() -> EmbeddingRuntime:
                api_key = os.environ.get(args.api_key_env, "").strip()
                if not api_key:
                    raise CalibrationError(
                        f"API key environment variable is unset: {args.api_key_env}"
                    )
                return OpenAIEmbeddingRuntime(
                    api_key,
                    model=prepared.snapshot.embedding_model,
                    dimensions=prepared.snapshot.embedding_dimension,
                    max_retries=1,
                )

            result = collect_query_embeddings(
                prepared.manifest,
                args.cache,
                budget_account_id=args.budget_account_id,
                budget_scope_id=args.budget_scope_id,
                ledger=ledger,
                embedder_factory=embedder_factory,
            )
            print(
                json.dumps(
                    {
                        "status": result.cache.status,
                        "reused": result.reused,
                        "cache": str(args.cache),
                        "manifest_sha256": result.cache.claim.manifest_sha256,
                        "provider_call_id": result.cache.claim.provider_call_id,
                    },
                    sort_keys=True,
                )
            )
            return 0

        cache = _read_cache(args.cache)
        policies = (
            tuple(_read_policy(path) for path in args.policy)
            if args.policy
            else default_candidate_grid(prepared.snapshot.retrieval_policy)
        )
        output = evaluate_policy_candidates(
            store,
            prepared,
            probes,
            access,
            cache,
            policies,
        )
        _write_exclusive_json(args.output, output)
        print(
            json.dumps(
                {
                    "status": "complete",
                    "output": str(args.output),
                    "candidate_count": len(output["candidates"]),
                    "separating_threshold_found": output["separating_threshold_found"],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
