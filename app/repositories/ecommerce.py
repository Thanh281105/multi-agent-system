"""Repository operations for products, shops, and reviews."""

from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop


class EcommerceRepository:
    """Database access object with no knowledge of ADK or LLMs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def search_products(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        max_price: int | None = None,
        min_price: int | None = None,
        min_rating: float | None = None,
        platform: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Return products matching structured filters and text search."""

        filters = []
        if query:
            pattern = f"%{query.strip()}%"
            filters.append(
                or_(
                    Product.name.ilike(pattern),
                    Product.category.ilike(pattern),
                    Product.description.ilike(pattern),
                    Shop.name.ilike(pattern),
                )
            )
        if category:
            filters.append(Product.category == category.strip())
        if max_price is not None:
            filters.append(Product.price <= max_price)
        if min_price is not None:
            filters.append(Product.price >= min_price)
        if min_rating is not None:
            filters.append(Product.rating >= min_rating)
        if platform:
            filters.append(Product.platform == platform.strip())

        statement = (
            select(Product)
            .join(Product.shop)
            .where(*filters)
            .options(joinedload(Product.shop))
            .order_by(
                Product.rating.desc(),
                Product.sold_count.desc(),
                Product.price.asc(),
                Product.id.asc(),
            )
            .limit(limit)
        )
        products = self.session.scalars(statement).all()
        return [_product_summary(product) for product in products]

    def get_product_by_id(self, product_id: int) -> Product | None:
        """Return one product and its shop, if it exists."""

        statement = (
            select(Product)
            .where(Product.id == product_id)
            .options(joinedload(Product.shop))
        )
        return self.session.scalars(statement).first()

    def get_product_reviews(
        self,
        *,
        product_id: int,
        limit: int = 20,
    ) -> tuple[Product | None, list[Review]]:
        """Return the product and its newest deterministic review slice."""

        product = self.get_product_by_id(product_id)
        if product is None:
            return None, []

        statement = (
            select(Review)
            .where(Review.product_id == product_id)
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(limit)
        )
        reviews = list(self.session.scalars(statement).all())
        return product, reviews

    def get_products_by_ids(self, product_ids: Sequence[int]) -> list[Product]:
        """Return products in the same order as the requested IDs."""

        if not product_ids:
            return []

        statement = (
            select(Product)
            .where(Product.id.in_(product_ids))
            .options(joinedload(Product.shop))
        )
        by_id = {product.id: product for product in self.session.scalars(statement)}
        return [by_id[product_id] for product_id in product_ids if product_id in by_id]


def _product_summary(product: Product) -> dict[str, object]:
    """Serialize a Product ORM object into a JSON-friendly fact record."""

    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "price": product.price,
        "rating": product.rating,
        "sold_count": product.sold_count,
        "shop": product.shop.name,
        "platform": product.platform,
    }


def product_comparison_fact(product: Product) -> dict[str, object]:
    """Serialize the additional facts needed by the comparison tool."""

    return {
        **_product_summary(product),
        "original_price": product.original_price,
        "description": product.description,
    }
