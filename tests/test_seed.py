import pytest
from sqlalchemy import func, select

from app.db import session as db_session
from app.db.seed import SeedConflictError, seed_database
from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop


def test_seed_is_complete_and_supports_demo_constraints() -> None:
    with db_session.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Shop)) == 5
        assert session.scalar(select(func.count()).select_from(Product)) == 30
        assert session.scalar(select(func.count()).select_from(Review)) == 150

        demo_products = session.scalars(
            select(Product).where(
                Product.category == "Tai nghe",
                Product.price <= 1_000_000,
                Product.rating >= 4.5,
            )
        ).all()
        assert len(demo_products) >= 2


def test_seed_is_idempotent_for_the_exact_sample_dataset() -> None:
    counts = seed_database(session_factory=db_session.SessionLocal)

    assert counts == {"shops": 5, "products": 30, "reviews": 150}


def test_seed_refuses_mixed_data_but_reset_is_explicit() -> None:
    with db_session.SessionLocal.begin() as session:
        session.add(
            Shop(
                id=99,
                name="Do not overwrite",
                platform="Shopee",
                rating=5,
            )
        )

    with pytest.raises(SeedConflictError):
        seed_database(session_factory=db_session.SessionLocal)

    counts = seed_database(session_factory=db_session.SessionLocal, reset=True)
    assert counts == {"shops": 5, "products": 30, "reviews": 150}
