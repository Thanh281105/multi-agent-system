"""Idempotently install the clearly labeled sample documents into Qdrant."""

from app.core.config import settings
from app.knowledge.qdrant import QdrantKnowledgeStore
from app.knowledge.sample import SAMPLE_MARKET_DOCUMENTS


def seed_sample_knowledge() -> int:
    store = QdrantKnowledgeStore(
        settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout_seconds=settings.qdrant_timeout_seconds,
    )
    store.ensure_collection()
    return store.upsert_documents(SAMPLE_MARKET_DOCUMENTS)


def main() -> None:
    count = seed_sample_knowledge()
    print(f"Sample knowledge verified: {count} documents")


if __name__ == "__main__":
    main()
