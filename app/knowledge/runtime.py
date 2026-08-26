"""Resolve one embedding algorithm and a non-interchangeable collection name."""

from __future__ import annotations

from app.core.config import Settings
from app.knowledge.embedding import HashingTextEmbedder
from app.shared import EmbeddingRuntime, OpenAIEmbeddingRuntime


def build_knowledge_embedder(config: Settings) -> EmbeddingRuntime:
    """Use OpenAI when selected/available; otherwise retain reproducible hashing."""

    key = config.openai_api_key_value
    use_openai = config.embedding_backend == "openai" or (
        config.embedding_backend == "auto" and bool(key)
    )
    if not use_openai:
        return HashingTextEmbedder()
    return OpenAIEmbeddingRuntime(
        key,
        model=config.openai_embedding_model,
        dimensions=config.openai_embedding_dimensions,
        timeout_seconds=config.openai_request_timeout_seconds,
        max_retries=config.openai_max_retries,
    )


def versioned_knowledge_collection(
    base_collection: str,
    embedder: EmbeddingRuntime,
) -> str:
    """Prevent vector queries from mixing incompatible embedding algorithms."""

    if embedder.method.startswith("openai_"):
        suffix = "_openai_v1"
        if not base_collection.endswith(suffix):
            candidate = f"{base_collection}{suffix}"
            if len(candidate) > 128:
                raise ValueError("versioned Qdrant collection name exceeds 128 chars")
            return candidate
    return base_collection
