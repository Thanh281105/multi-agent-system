"""Qdrant REST contract, deterministic embedding, and readiness tests."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.knowledge import (
    SAMPLE_MARKET_DOCUMENTS,
    HashingTextEmbedder,
    KnowledgeStoreContractError,
    KnowledgeStoreUnavailableError,
    QdrantKnowledgeStore,
)
from app.main import create_app


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

    first = embedder.embed("tai nghe pin tốt")
    second = embedder.embed("tai nghe pin tốt")

    assert first == second
    assert len(first) == 64
    assert math.isclose(sum(value * value for value in first), 1, rel_tol=1e-9)
    with pytest.raises(ValueError, match="at least one token"):
        embedder.embed("---")


def test_qdrant_seed_and_query_follow_versioned_rest_contract() -> None:
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
                                    "document_id": "sample_market_audio_2026",
                                    "title": "Tín hiệu tai nghe",
                                    "content": "Dữ liệu mẫu về âm thanh và pin.",
                                    "source": "sample_thesis_dataset",
                                    "sample_data": True,
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
        collection="sample_market_knowledge",
        api_key="secret-not-logged",
        embedder=HashingTextEmbedder(dimensions=64),
        transport=transport,
        request_retries=0,
    )

    store.ensure_collection()
    inserted = store.upsert_documents(SAMPLE_MARKET_DOCUMENTS)
    result = store.search("tai nghe pin", limit=2)

    assert inserted == 3
    assert result == {
        "count": 1,
        "documents": [
            {
                "id": "sample_market_audio_2026",
                "title": "Tín hiệu tai nghe",
                "content": "Dữ liệu mẫu về âm thanh và pin.",
                "score": 0.812346,
                "source": "sample_thesis_dataset",
                "sample_data": True,
            }
        ],
        "query": "tai nghe pin",
        "method": "qdrant_hashed_token_cosine_v1",
    }
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/collections/sample_market_knowledge"),
        ("PUT", "/collections/sample_market_knowledge"),
        ("PUT", "/collections/sample_market_knowledge/points?wait=true"),
        ("POST", "/collections/sample_market_knowledge/points/query"),
    ]
    create_payload = transport.calls[1][2]
    assert create_payload == {"vectors": {"size": 64, "distance": "Cosine"}}
    points = transport.calls[2][2]["points"]
    assert len(points) == 3
    assert len(points[0]["vector"]) == 64
    assert points[0]["payload"]["sample_data"] is True


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


def test_qdrant_readiness_requires_compatible_seeded_collection() -> None:
    compatible_collection = {
        "status": "ok",
        "result": {
            "config": {"params": {"vectors": {"size": 64, "distance": "Cosine"}}},
            "points_count": 3,
        },
    }
    transport = ScriptedTransport(
        [(200, "healthz check passed"), (200, compatible_collection)]
    )
    store = QdrantKnowledgeStore(
        "http://qdrant:6333",
        collection="sample_market_knowledge",
        embedder=HashingTextEmbedder(dimensions=64),
        transport=transport,
        request_retries=0,
    )

    assert store.ready() is True
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/readyz"),
        ("GET", "/collections/sample_market_knowledge"),
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

    with pytest.raises(KnowledgeStoreContractError, match="seeded knowledge"):
        store.ready()


def test_qdrant_transient_failure_is_bounded_and_readiness_reports_it() -> None:
    transport = ScriptedTransport([(503, {}), (503, {})])
    unavailable = QdrantKnowledgeStore(
        "http://qdrant:6333",
        transport=transport,
        request_retries=1,
    )
    with pytest.raises(KnowledgeStoreUnavailableError, match="HTTP status 503"):
        unavailable.search("tai nghe")
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
        "qdrant": "failed",
    }


def test_production_requires_authenticated_qdrant() -> None:
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
    with pytest.raises(ValueError, match="KNOWLEDGE_BACKEND=qdrant"):
        Settings(**base)
    with pytest.raises(ValueError, match="QDRANT_API_KEY"):
        Settings(**base, knowledge_backend="qdrant")

    configured = Settings(
        **base,
        knowledge_backend="qdrant",
        qdrant_api_key="strong-qdrant-key",
    )
    runtime = create_app(configured).state.gateway_runtime
    assert runtime.knowledge_store is not None
