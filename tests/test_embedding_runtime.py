"""OpenAI embedding adapter contract tests without network calls."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.knowledge.runtime import versioned_knowledge_collection
from app.shared import EmbeddingRuntimeError, OpenAIEmbeddingRuntime


class FakeEmbeddings:
    def __init__(self, data: list[Any]) -> None:
        self.data = data
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(data=self.data)


class FakeClient:
    def __init__(self, data: list[Any]) -> None:
        self.embeddings = FakeEmbeddings(data)


def test_embedding_runtime_preserves_provider_indexes_and_dimensions() -> None:
    client = FakeClient(
        [
            SimpleNamespace(index=1, embedding=[0.0, 1.0] * 16),
            SimpleNamespace(index=0, embedding=[1.0, 0.0] * 16),
        ]
    )
    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        dimensions=32,
        client=client,
    )

    vectors = runtime.embed_many(["một", "hai"])

    assert vectors[0][0] == 1.0
    assert vectors[1][1] == 1.0
    assert client.embeddings.requests[0]["model"] == "text-embedding-3-small"
    assert client.embeddings.requests[0]["dimensions"] == 32
    assert "test-key" not in repr(client.embeddings.requests[0])


def test_embedding_runtime_rejects_invalid_vectors_and_blank_input() -> None:
    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        dimensions=32,
        client=FakeClient([SimpleNamespace(index=0, embedding=[1.0])]),
    )

    with pytest.raises(ValueError, match="non-blank"):
        runtime.embed(" ")
    with pytest.raises(EmbeddingRuntimeError, match="dimension"):
        runtime.embed("valid")


def test_openai_vectors_use_a_non_interchangeable_collection_version() -> None:
    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        dimensions=32,
        client=FakeClient([]),
    )

    assert versioned_knowledge_collection("market_notes", runtime) == (
        "market_notes_openai_v1"
    )
    assert versioned_knowledge_collection("market_notes_openai_v1", runtime) == (
        "market_notes_openai_v1"
    )
