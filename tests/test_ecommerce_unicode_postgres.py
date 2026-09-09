"""PostgreSQL Unicode author matching over actual JSON-encoded metadata."""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.migrate import upgrade_database
from app.models.product import Product
from app.repositories.ecommerce import EcommerceRepository
from tests.v2_postgres_support import disposable_postgres_database


@pytest.fixture(scope="module")
def unicode_sessions() -> Iterator[sessionmaker[Session]]:
    with disposable_postgres_database("thanh_v2_p2_unicode_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url)
        sessions = sessionmaker(bind=engine)
        with sessions() as session:
            for index, author, price in (
                (1, "Nguyễn An", 90_000),
                (2, "Nguyễn An", 160_000),
                (3, "Alice", 40_000),
            ):
                session.add(
                    Product(
                        id=index,
                        name=f"Synthetic title {index}",
                        authors=[author],
                        category="Công nghệ",
                        price=price,
                        rating=4.0 + index / 10,
                        sold_count=index,
                        description="Synthetic fixture.",
                        platform="Tiki",
                    )
                )
            session.commit()
        try:
            yield sessions
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    "search_kwargs", [{"author": "Nguyễn An"}, {"query": "Nguyễn An"}]
)
def test_unicode_author_matches_before_budget_and_limit(
    unicode_sessions: sessionmaker[Session],
    search_kwargs: dict[str, str],
) -> None:
    with unicode_sessions() as session:
        result = EcommerceRepository(session).search_products(
            **search_kwargs,
            max_price=100_000,
            limit=1,
        )
    assert [row["id"] for row in result] == [1]


def test_unicode_market_statistics_and_ascii_search_remain_consistent(
    unicode_sessions: sessionmaker[Session],
) -> None:
    with unicode_sessions() as session:
        repository = EcommerceRepository(session)
        statistics = repository.get_product_statistics(author="Nguyễn An")
        assert statistics["product_count"] == 2
        assert statistics["min_price"] == 90_000
        assert statistics["max_price"] == 160_000
        assert statistics["author_distribution"] == {"Nguyễn An": 2}
        assert [row["id"] for row in repository.search_products(author="Alice")] == [3]
        assert repository.search_products(author="Unknown author") == []
