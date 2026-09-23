"""Composition tests for the lazy, server-authoritative v2 runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.contracts import AuthorizationContext
from app.core.config import Settings
from app.gateway.runtime import build_gateway_runtime
from app.knowledge.v2_contracts import (
    IndexBuildSpec,
    PublishedKnowledgeSnapshot,
    RetrievalPolicy,
)
from app.shared import EmbeddingRuntime, ModelRuntime
from app.shared.budget import PricingManifest, default_pricing_manifest_path
from app.v2.contracts import ChatRequest, ConversationMode
from app.v2.runtime import V2RuntimeConfigurationError, V2RuntimeFactory
from app.v2.tools import CatalogSnapshot

CORPUS_ID = f"cor_{'a' * 60}"
INDEX_ID = f"idx_{'b' * 60}"


class _EmbeddingRuntime:
    method = "hashed_token_cosine_v1"
    dimensions = 128

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError(f"provider work was dispatched for {texts!r}")


class _KnowledgeStore:
    def __init__(self, snapshot: PublishedKnowledgeSnapshot) -> None:
        self.snapshot = snapshot
        self.resolve_calls: list[tuple[str, str | None]] = []

    def resolve_published_snapshot(
        self,
        corpus_version_id: str,
        *,
        index_manifest_id: str | None = None,
    ) -> PublishedKnowledgeSnapshot:
        self.resolve_calls.append((corpus_version_id, index_manifest_id))
        return self.snapshot


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "model_runtime_mode": "off",
        "embedding_backend": "hashing",
        "knowledge_backend": "disabled",
        "shared_state_backend": "memory",
    }
    values.update(overrides)
    return Settings(**values)


def _snapshot() -> PublishedKnowledgeSnapshot:
    spec = IndexBuildSpec(
        embedding_model="hashed_token_cosine_v1",
        embedding_dimension=128,
        chunker_version="chunker_v1",
        enrichment_policy_version="enrichment_v1",
    )
    return PublishedKnowledgeSnapshot(
        corpus_version_id=CORPUS_ID,
        corpus_name="runtime-test",
        corpus_version="1",
        index_manifest_id=INDEX_ID,
        index_fingerprint=spec.index_fingerprint,
        embedding_model=spec.embedding_model,
        embedding_dimension=spec.embedding_dimension,
        chunker_version=spec.chunker_version,
        enrichment_policy_version=spec.enrichment_policy_version,
        query_embedding_fingerprint=spec.query_embedding_fingerprint,
        retrieval_policy=RetrievalPolicy(
            version="policy_v1",
            dense_weight=1.0,
            lexical_weight=1.0,
            min_dense_relevance=0.1,
            min_lexical_coverage=0.1,
        ),
        published_at=datetime(2026, 9, 13, tzinfo=UTC),
    )


def _injected_factory(
    *,
    session_factory_builder: Any | None = None,
) -> tuple[V2RuntimeFactory, _KnowledgeStore, list[str]]:
    snapshot = _snapshot()
    store = _KnowledgeStore(snapshot)
    catalog_calls: list[str] = []

    def forbidden_session() -> Session:
        raise AssertionError("service construction opened a database session")

    def load_catalog(session_factory: Any) -> CatalogSnapshot:
        assert session_factory is forbidden_session
        catalog_calls.append("catalog")
        return CatalogSnapshot(
            version_id="catalog_runtime_v1",
            observed_at=datetime(2026, 9, 13, tzinfo=UTC),
            source_ids=(1,),
        )

    factory = V2RuntimeFactory(
        _settings(
            v2_corpus_version_id=CORPUS_ID,
            v2_index_manifest_id=INDEX_ID,
            v2_budget_account_id="runtime-budget",
        ),
        session_factory=forbidden_session,
        session_factory_builder=session_factory_builder,
        model_runtime=cast(ModelRuntime, object()),
        embedding_runtime=cast(EmbeddingRuntime, _EmbeddingRuntime()),
        catalog_snapshot_loader=load_catalog,
        knowledge_store_factory=lambda supplied: (
            store if supplied is forbidden_session else pytest.fail("wrong session")
        ),
        pricing_manifest_loader=lambda: PricingManifest.load(
            default_pricing_manifest_path()
        ),
    )
    return factory, store, catalog_calls


def test_gateway_and_v2_factory_construction_do_not_resolve_dependencies() -> None:
    calls: list[str] = []

    def forbidden_builder(_: str) -> Any:
        calls.append("database")
        raise AssertionError("database factory was built eagerly")

    factory = V2RuntimeFactory(
        _settings(),
        session_factory_builder=forbidden_builder,
        embedding_runtime_factory=lambda _: pytest.fail(
            "embedding provider was built eagerly"
        ),
        catalog_snapshot_loader=lambda _: pytest.fail(
            "catalog snapshot was queried eagerly"
        ),
        knowledge_store_factory=lambda _: pytest.fail(
            "knowledge store was built eagerly"
        ),
        pricing_manifest_loader=lambda: pytest.fail(
            "pricing manifest was loaded eagerly"
        ),
    )
    gateway = build_gateway_runtime(
        _settings(),
        v2_runtime_factory=factory,
    )

    assert gateway.v2_runtime_factory is factory
    assert not factory.initialized
    assert calls == []
    assert gateway.orchestrator is not None
    assert gateway.turns is not None


def test_create_app_leaves_the_default_v2_factory_dormant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.v2.runtime as runtime_module
    from app.main import create_app

    monkeypatch.setattr(
        runtime_module,
        "_default_session_factory_builder",
        lambda _: pytest.fail("create_app built a database dependency"),
    )
    monkeypatch.setattr(
        runtime_module,
        "build_knowledge_embedder",
        lambda _: pytest.fail("create_app built an embedding provider"),
    )

    application = create_app(_settings())

    factory = application.state.gateway_runtime.v2_runtime_factory
    assert isinstance(factory, V2RuntimeFactory)
    assert not factory.initialized


def test_injected_dependencies_build_one_pinned_service_graph() -> None:
    factory, store, catalog_calls = _injected_factory()
    first_authorization = AuthorizationContext(
        tenant_id="tenant_one",
        principal_id="principal-one",
        scopes=frozenset({"ecommerce.read"}),
    )
    second_authorization = AuthorizationContext(
        tenant_id="tenant_two",
        principal_id="principal-two",
        scopes=frozenset({"ecommerce.read"}),
    )

    first = factory.resolve_for_request(
        first_authorization,
        mode=ConversationMode.SHOPPER,
    )
    second = factory.resolve_for_request(
        second_authorization,
        mode=ConversationMode.SHOPPER,
    )

    assert factory.initialized
    assert first.services is second.services
    assert catalog_calls == ["catalog"]
    assert store.resolve_calls == [(CORPUS_ID, INDEX_ID)]
    assert first.versions.catalog_version_id == "catalog_runtime_v1"
    assert first.versions.corpus_version_id == CORPUS_ID
    assert first.versions.index_manifest_id == INDEX_ID
    assert first.services.budget_account_id == "runtime-budget"
    assert first.services.operation_executor.dispatcher is first.services.read_tools
    assert first.services.supervisor.operation_executor is (
        first.services.operation_executor
    )
    assert first.executor is first.services.executor
    assert first.services.turn_service.executor is first.executor
    assert first.access.binding.tenant_id == "tenant_one"
    assert second.access.binding.tenant_id == "tenant_two"
    assert first.access.binding.store_id == "demo"
    assert first.model_snapshot.planning_model == "gpt-5.4-mini"
    assert first.model_snapshot.embedding_model == "hashed_token_cosine_v1"


def test_public_input_cannot_override_server_authority_or_snapshots() -> None:
    factory, _, _ = _injected_factory()
    authorization = AuthorizationContext(
        tenant_id="tenant_trusted",
        principal_id="principal-trusted",
        scopes=frozenset({"ecommerce.read"}),
    )

    resolved = factory.resolve_for_request(
        authorization,
        mode=ConversationMode.SHOPPER,
    )
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        factory.resolve_for_request(  # type: ignore[call-arg]
            authorization,
            mode=ConversationMode.SHOPPER,
            tenant_id="tenant_attacker",
            corpus_version_id="corpus_attacker",
            planning_model="attacker-model",
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ChatRequest.model_validate(
            {
                "conversation_id": "conversation_runtime",
                "client_turn_id": "client-runtime",
                "message": "Find a book",
                "tenant_id": "tenant_attacker",
                "corpus_version_id": "corpus_attacker",
                "planning_model": "attacker-model",
            }
        )

    assert resolved.access.binding.tenant_id == "tenant_trusted"
    assert resolved.access.binding.principal_id == "principal-trusted"
    assert resolved.access.binding.store_id == "demo"
    assert resolved.versions.corpus_version_id == CORPUS_ID
    assert resolved.versions.index_manifest_id == INDEX_ID
    assert resolved.model_snapshot.planning_model == "gpt-5.4-mini"


def test_v2_configuration_errors_are_clear_and_remain_lazy() -> None:
    with pytest.raises(
        ValidationError,
        match="V2_INDEX_MANIFEST_ID requires V2_CORPUS_VERSION_ID",
    ):
        _settings(v2_index_manifest_id=INDEX_ID)

    calls: list[str] = []

    def forbidden_builder(_: str) -> Any:
        calls.append("database")
        raise AssertionError("missing configuration must fail before database setup")

    factory = V2RuntimeFactory(
        _settings(),
        session_factory_builder=forbidden_builder,
    )
    authorization = AuthorizationContext(
        tenant_id="tenant_config",
        principal_id="principal-config",
        scopes=frozenset({"ecommerce.read"}),
    )

    with pytest.raises(
        V2RuntimeConfigurationError,
        match="V2_CORPUS_VERSION_ID is required",
    ) as error:
        factory.resolve_for_request(
            authorization,
            mode=ConversationMode.SHOPPER,
        )

    assert error.value.code == "v2_corpus_version_not_configured"
    assert not factory.initialized
    assert calls == []
