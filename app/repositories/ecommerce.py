import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop

_CATEGORY_PREFIX_PATTERN = re.compile(
    r"^(?:sách|cuốn sách|quyển sách|tai nghe|điện thoại|laptop|máy tính|sản phẩm)\s+",
    flags=re.IGNORECASE,
)


class EcommerceRepository:
    """Database access object with no knowledge of an LLM provider."""

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

        def _build_statement(search_text: str | None) -> Any:
            filters = []
            if search_text:
                pattern = f"%{search_text.strip()}%"
                filters.append(
                    or_(
                        Product.name.ilike(pattern),
                        Product.category.ilike(pattern),
                        Product.description.ilike(pattern),
                        Product.seller_name.ilike(pattern),
                        Shop.name.ilike(pattern),
                    )
                )
            if category:
                filters.append(Product.category.ilike(category.strip()))
            if max_price is not None:
                filters.append(Product.price <= max_price)
            if min_price is not None:
                filters.append(Product.price >= min_price)
            if min_rating is not None:
                filters.append(Product.rating >= min_rating)
            if platform:
                filters.append(Product.platform.ilike(platform.strip()))

            return (
                select(Product)
                .outerjoin(Product.shop)
                .where(*filters)
                .options(
                    joinedload(Product.shop),
                    joinedload(Product.dataset_source),
                )
                .order_by(
                    Product.rating.desc().nullslast(),
                    Product.sold_count.desc().nullslast(),
                    Product.price.asc(),
                    Product.id.asc(),
                )
                .limit(limit)
            )

        products = self.session.scalars(_build_statement(query)).all()
        if not products and query:
            cleaned = _CATEGORY_PREFIX_PATTERN.sub("", query.strip()).strip()
            if cleaned and cleaned.casefold() != query.strip().casefold():
                products = self.session.scalars(_build_statement(cleaned)).all()

        return [_product_summary(product) for product in products]

    def get_product_by_id(self, product_id: int) -> Product | None:
        """Return one product and its shop, if it exists."""

        statement = (
            select(Product)
            .where(Product.id == product_id)
            .options(
                joinedload(Product.shop),
                joinedload(Product.dataset_source),
            )
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
            .order_by(Review.created_at.desc().nullslast(), Review.id.desc())
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
            .options(
                joinedload(Product.shop),
                joinedload(Product.dataset_source),
            )
        )
        by_id = {product.id: product for product in self.session.scalars(statement)}
        return [by_id[product_id] for product_id in product_ids if product_id in by_id]

    def get_product_statistics(
        self,
        *,
        category: str | None = None,
    ) -> dict[str, object]:
        """Return aggregate facts used by product and market intelligence."""

        filters = []
        if category:
            filters.append(Product.category.ilike(category.strip()))

        totals = self.session.execute(
            select(
                func.count(Product.id),
                func.min(Product.price),
                func.max(Product.price),
                func.avg(Product.price),
                func.avg(Product.rating),
                func.sum(Product.sold_count),
            ).where(*filters)
        ).one()
        platform_rows = self.session.execute(
            select(Product.platform, func.count(Product.id))
            .where(*filters)
            .group_by(Product.platform)
            .order_by(Product.platform)
        ).all()
        category_rows = self.session.execute(
            select(Product.category, func.count(Product.id))
            .where(*filters)
            .group_by(Product.category)
            .order_by(Product.category)
        ).all()
        source_rows = self.session.execute(
            select(
                Product.source_id,
                DatasetSource.dataset_id,
                DatasetSource.dataset_version,
                DatasetSource.profile,
            )
            .outerjoin(DatasetSource, Product.source_id == DatasetSource.id)
            .where(*filters)
            .distinct()
            .order_by(Product.source_id)
        ).all()

        return {
            "category": category.strip() if category else None,
            "product_count": int(totals[0] or 0),
            "min_price": int(totals[1]) if totals[1] is not None else None,
            "max_price": int(totals[2]) if totals[2] is not None else None,
            "average_price": (
                round(float(totals[3]), 2) if totals[3] is not None else None
            ),
            "average_rating": (
                round(float(totals[4]), 3) if totals[4] is not None else None
            ),
            "total_sold": int(totals[5] or 0),
            "platform_distribution": {
                str(platform): int(count) for platform, count in platform_rows
            },
            "category_distribution": {
                str(category_name): int(count) for category_name, count in category_rows
            },
            "provenance": [
                _source_provenance(
                    source_id=source_id,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    dataset_profile=dataset_profile,
                    fields=("price", "rating", "sold_count"),
                )
                for (
                    source_id,
                    dataset_id,
                    dataset_version,
                    dataset_profile,
                ) in source_rows
            ],
        }


def _product_summary(product: Product) -> dict[str, object]:
    """Serialize a Product ORM object into a JSON-friendly fact record."""

    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "price": product.price,
        "rating": product.rating,
        "sold_count": product.sold_count,
        "shop": product.shop.name if product.shop is not None else product.seller_name,
        "platform": product.platform,
        "provenance": product_provenance(product),
    }


def product_comparison_fact(product: Product) -> dict[str, object]:
    """Serialize the additional facts needed by the comparison tool."""

    return {
        **_product_summary(product),
        "original_price": product.original_price,
        "description": product.description,
    }


def product_provenance(
    product: Product,
    *,
    fields: tuple[str, ...] = ("price", "rating", "sold_count", "shop", "platform"),
    fallback_source_id: str = "postgresql:products",
) -> dict[str, object]:
    """Return safe provenance metadata for facts belonging to one product."""

    source = product.dataset_source
    return _source_provenance(
        source_id=product.source_id,
        dataset_id=source.dataset_id if source is not None else None,
        dataset_version=source.dataset_version if source is not None else None,
        dataset_profile=source.profile if source is not None else None,
        fields=fields,
        fallback_source_id=fallback_source_id,
    )


def _source_provenance(
    *,
    source_id: int | None,
    dataset_id: str | None,
    dataset_version: str | None,
    dataset_profile: str | None,
    fields: tuple[str, ...],
    fallback_source_id: str = "postgresql:products",
) -> dict[str, object]:
    if source_id is not None and dataset_id and dataset_version and dataset_profile:
        return {
            "source_type": "sample.public_dataset",
            "source_id": f"{dataset_id}:{dataset_version}:{dataset_profile}",
            "fields": list(fields),
            "sample_data": True,
        }
    return {
        "source_type": "sample.database",
        "source_id": fallback_source_id,
        "fields": list(fields),
        "sample_data": True,
    }
