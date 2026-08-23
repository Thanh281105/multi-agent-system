"""Idempotently install the clearly labeled sample documents into Qdrant."""

import argparse
import time

from app.core.config import settings
from app.knowledge.qdrant import (
    KnowledgeStoreUnavailableError,
    QdrantKnowledgeStore,
)
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


def wait_and_seed_sample_knowledge(*, wait_seconds: float) -> int:
    """Wait for Qdrant startup while preserving a bounded failure deadline."""

    if wait_seconds < 0 or wait_seconds > 300:
        raise ValueError("wait_seconds must be between 0 and 300")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            return seed_sample_knowledge()
        except KnowledgeStoreUnavailableError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install the deterministic sample knowledge collection.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=0,
        help="bounded time to wait for Qdrant startup",
    )
    arguments = parser.parse_args()
    count = wait_and_seed_sample_knowledge(wait_seconds=arguments.wait_seconds)
    print(f"Sample knowledge verified: {count} documents")


if __name__ == "__main__":
    main()
