"""Provider-neutral text embedding boundary for versioned vector indexes."""

from __future__ import annotations

import math
from typing import Any, Protocol

from openai import OpenAI


class EmbeddingRuntimeError(RuntimeError):
    """Raised when an embedding provider violates the local contract."""


class EmbeddingRuntime(Protocol):
    """Synchronous contract used inside MCP worker threads."""

    dimensions: int
    method: str

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingRuntime:
    """Validated OpenAI embeddings adapter preserving input order."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "text-embedding-3-small",
        dimensions: int = 1_536,
        timeout_seconds: float = 18.0,
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip() and client is None:
            raise ValueError("OpenAIEmbeddingRuntime requires an API key")
        if not model.strip():
            raise ValueError("embedding model must not be blank")
        if not 32 <= dimensions <= 3_072:
            raise ValueError("embedding dimensions must be between 32 and 3072")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        self.model = model
        self.dimensions = dimensions
        self.method = f"openai_{model}_{dimensions}_v1"
        self._timeout_seconds = timeout_seconds
        self._client = client or OpenAI(api_key=api_key, max_retries=max_retries)

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        cleaned = [text.strip() for text in texts]
        if not cleaned or any(not text for text in cleaned):
            raise ValueError("embedding inputs must contain non-blank text")
        try:
            response = self._client.embeddings.create(
                model=self.model,
                input=cleaned,
                dimensions=self.dimensions,
                encoding_format="float",
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            raise EmbeddingRuntimeError("embedding_provider_failed") from exc

        ordered = sorted(response.data, key=lambda item: int(item.index))
        if len(ordered) != len(cleaned):
            raise EmbeddingRuntimeError("embedding_cardinality_mismatch")
        if [int(item.index) for item in ordered] != list(range(len(cleaned))):
            raise EmbeddingRuntimeError("embedding_order_mismatch")

        vectors: list[list[float]] = []
        for item in ordered:
            vector = [float(value) for value in item.embedding]
            if len(vector) != self.dimensions:
                raise EmbeddingRuntimeError("embedding_dimension_mismatch")
            if any(not math.isfinite(value) for value in vector):
                raise EmbeddingRuntimeError("embedding_contains_non_finite_value")
            vectors.append(vector)
        return vectors
