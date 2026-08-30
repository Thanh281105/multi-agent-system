"""Qdrant REST contract, deterministic embedding, and readiness tests."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.knowledge import (
    DisabledKnowledgeStore,
    HashingTextEmbedder,
    KnowledgeStoreContractError,
    KnowledgeStoreUnavailableError,
    QdrantKnowledgeStore,
)
from app.main import create_app

TEST_DOCUMENTS: tuple[dict[str, object], ...] = (
    {
        "id": "book-selection-guide",
        "title": "Cách chọn sách phù hợp",
        "content": "Chọn sách theo tác giả, chủ đề và nhu cầu đọc.",
        "source": "test_fixture",
        "sample_data": False,
    },
)


class ScriptedTransport:
    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> tuple[int, Any]:
        self.calls.append((method, path, payload))
        if not self.responses:
            raise AssertionError("unexpected Qdrant request")
        return self.responses.pop(0)


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashingTextEmbedder(dimensions=64)

    first = embedder.embed("sách lịch sử Việt Nam")
    second = embedder.embed("sách lịch sử Việt Nam")

    assert first == second
    assert len(first) == 64
    assert math.isclose(sum(value * value for value in first), 1, rel_tol=1e-9)
    with pytest.raises(ValueError, match="at least one token"):
        embedder.embed("---")


def test_qdrant_upsert_and_query_follow_versioned_rest_contract() -> None:
    transport = ScriptedTransport(
        [
            (404, {"status": "error"}),
            (200, {"status": "ok", "result": True}),
            (200, {"status": "ok", "result": {"status": "completed"}}),
            (
                200,
                {
                    "status": "ok",
                    "result": {
                        "points": [
                            {
                                "score": 0.81234567,
                                "payload": {
                                    "document_id": "book-selection-guide",
                                    "title": "Cách chọn sách phù hợp",
                                    "content": "Chọn theo tác giả và chủ đề.",
                                    "source": "test_fixture",
                                    "sample_data": False,
                                },
                            }
                        ]
                    },
                },
            ),
        ]
    )
    store = QdrantKnowledgeStore(
        "http://qdrant:6333",
        collection="knowledge",
        api_key="secret-not-logged",
        embedder=HashingTextEmbedder(dimensions=64),
        transport=transport,
        request_retries=0,
    )

    store.ensure_collection()
    inserted = store.upsert_documents(TEST_DOCUMENTS)
    result = store.search("sách theo tác giả", limit=2)

    assert inserted == 1
    assert result == {
        "count": 1,
        "documents": [
            {
                "id": "book-selection-guide",
                "title": "Cách chọn sách phù hợp",
                "content": "Chọn theo tác giả và chủ đề.",
                "score": 0.812346,
                "source": "test_fixture",
                "sample_data": False,
            }
        ],
        "query": "sách theo tác giả",
        "method": "qdrant_hashed_token_cosine_v1",
    }
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/collections/knowledge"),
        ("PUT", "/collections/knowledge"),
        ("PUT", "/collections/knowledge/points?wait=true"),
        ("POST", "/collections/knowledge/points/query"),
    ]
    create_payload = transport.calls[1][2]
    assert create_payload == {"vectors": {"size": 64, "distance": "Cosine"}}
    points = transport.calls[2][2]["points"]
    assert len(points) == 1
    assert len(points[0]["vector"]) == 64
    assert points[0]["payload"]["sample_data"] is False


def test_existing_collection_dimension_mismatch_fails_closed() -> None:
    transport = ScriptedTransport(
        [
            (
                200,
                {
                    "status": "ok",
                    "result": {
                        "config": {
                            "params": {"vectors": {"size": 32, "distance": "Cosine"}}
                        }
                    },
                },
            )
        ]
    )
    store = QdrantKnowledgeStore(
        "http://qdrant:6333",
        embedder=HashingTextEmbedder(dimensions=64),
        transport=transport,
        request_retries=0,
    )

    with pytest.raises(KnowledgeStoreContractError, match="does not match"):
        store.ensure_collection()


def test_qdrant_readiness_requires_compatible_non_empty_collection() -> None:
    compatible_collection = {
        "status": "ok",
        "result": {
            "config": {"params": {"vectors": {"size": 64, "distance": "Cosine"}}},
            "points_count": 1,
        },
    }
    transport = ScriptedTransport(
        [(200, "healthz check passed"), (200, compatible_collection)]
    )
    store = QdrantKnowledgeStore(
        "http://qdrant:6333",
        collection="knowledge",
        embedder=HashingTextEmbedder(dimensions=64),
        transport=transport,
        request_retries=0,
    )

    assert store.ready() is True
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/readyz"),
        ("GET", "/collections/knowledge"),
    ]


def test_qdrant_readiness_rejects_empty_collection() -> None:
    transport = ScriptedTransport(
        [
            (200, "healthz check passed"),
            (
                200,
                {
                    "status": "ok",
                    "result": {
                        "config": {
                            "params": {"vectors": {"size": 128, "distance": "Cosine"}}
                        },
                        "points_count": 0,
                    },
                },
            ),
        ]
    )
    store = QdrantKnowledgeStore(
        "http://qdrant:6333",
        transport=transport,
        request_retries=0,
    )

    with pytest.raises(KnowledgeStoreContractError, match="knowledge points"):
        store.ready()


def test_qdrant_transient_failure_is_bounded_and_readiness_reports_it() -> None:
    transport = ScriptedTransport([(503, {}), (503, {})])
    unavailable = QdrantKnowledgeStore(
        "http://qdrant:6333",
        transport=transport,
        request_retries=1,
    )
    with pytest.raises(KnowledgeStoreUnavailableError, match="HTTP status 503"):
        unavailable.search("sách")
    assert len(transport.calls) == 2

    application = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            gateway_api_keys="test:test-secret-key",
        )
    )
    runtime = application.state.gateway_runtime
    failing_health = QdrantKnowledgeStore(
        "http://qdrant:6333",
        transport=ScriptedTransport([(503, {}), (503, {}), (503, {})]),
    )
    application.state.gateway_runtime = replace(
        runtime,
        knowledge_store=failing_health,
    )

    readiness = TestClient(application).get("/readyz")

    assert readiness.status_code == 503
    assert readiness.json()["checks"] == {
        "runtime": "ok",
        "database": "ok",
        "knowledge": "failed",
    }


def test_disabled_is_default_and_production_qdrant_remains_authenticated() -> None:
    base = {
        "_env_file": None,
        "app_env": "production",
        "database_url": (
            "postgresql+psycopg://ecommerce:strong-db-password@localhost/ecommerce"
        ),
        "gateway_api_keys": "production:strong-production-key",
        "legacy_chat_enabled": False,
        "shared_state_backend": "redis",
        "redis_url": "redis://:strong-redis-password@localhost:6379/0",
        "operations_api_key": "strong-operations-key",
        "model_runtime_mode": "off",
        "embedding_backend": "hashing",
    }
    disabled = Settings(**base)
    legacy_static = Settings.model_validate({"knowledge_backend": "static"})
    disabled_runtime = create_app(disabled).state.gateway_runtime

    assert disabled.knowledge_backend == "disabled"
    assert legacy_static.knowledge_backend == "disabled"
    assert isinstance(disabled_runtime.knowledge_store, DisabledKnowledgeStore)
    assert disabled_runtime.embedding_runtime is None

    with pytest.raises(ValueError, match="QDRANT_API_KEY"):
        Settings(**base, knowledge_backend="qdrant")

    configured = Settings(
        **base,
        knowledge_backend="qdrant",
        qdrant_api_key="strong-qdrant-key",
    )
    runtime = create_app(configured).state.gateway_runtime
    assert isinstance(runtime.knowledge_store, QdrantKnowledgeStore)
    assert runtime.embedding_runtime is not None


def test_create_app_accepts_a_narrow_future_knowledge_adapter() -> None:
    class ReadyKnowledgeStore:
        backend = "rag_test"

        def __bool__(self) -> bool:
            return False

        def ready(self) -> bool:
            return True

    injected = ReadyKnowledgeStore()
    application = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            gateway_api_keys="test:test-secret-key",
        ),
        knowledge_store=injected,
    )

    assert application.state.gateway_runtime.knowledge_store is injected
    readiness = TestClient(application).get("/readyz")
    assert readiness.status_code == 200
    assert readiness.json()["checks"]["knowledge"] == "ok"


def test_knowledge_readiness_rejects_false_without_overwriting_core_checks() -> None:
    class NotReadyKnowledgeStore:
        backend = "database"

        def ready(self) -> bool:
            return False

    application = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            gateway_api_keys="test:test-secret-key",
        ),
        knowledge_store=NotReadyKnowledgeStore(),
    )

    readiness = TestClient(application).get("/readyz")

    assert readiness.status_code == 503
    assert readiness.json()["checks"] == {
        "runtime": "ok",
        "database": "ok",
        "knowledge": "failed",
    }
