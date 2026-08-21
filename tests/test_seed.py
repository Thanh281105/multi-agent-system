from sqlalchemy import func, select

from app.db import session as db_session
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
