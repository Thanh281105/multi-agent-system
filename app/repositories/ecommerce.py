import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review

_CATEGORY_PREFIX_PATTERN = re.compile(
    r"^(?:sách|cuốn sách|quyển sách|cuốn|quyển)\s+",
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
        author: str | None = None,
        publisher: str | None = None,
        min_page_count: int | None = None,
        max_page_count: int | None = None,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Return products matching structured filters and text search."""

        python_metadata_filter = self.session.get_bind().dialect.name == "sqlite" and (
            author is not None or publisher is not None
        )

        def _build_statement(search_text: str | None) -> Any:
            filters = []
            if search_text:
                pattern = f"%{search_text.strip()}%"
                filters.append(
                    or_(
                        Product.name.ilike(pattern),
                        cast(Product.authors, String).ilike(pattern),
                        Product.publisher.ilike(pattern),
                        Product.category.ilike(pattern),
                        Product.description.ilike(pattern),
                    )
                )
            filters.append(Product.platform == "Tiki")
            if category:
                filters.append(Product.category.ilike(category.strip()))
            if author and not python_metadata_filter:
                filters.append(
                    cast(Product.authors, String).ilike(f"%{author.strip()}%")
                )
            if publisher and not python_metadata_filter:
                filters.append(Product.publisher.ilike(f"%{publisher.strip()}%"))
            if max_price is not None:
                filters.append(Product.price <= max_price)
            if min_price is not None:
                filters.append(Product.price >= min_price)
            if min_rating is not None:
                filters.append(Product.rating >= min_rating)
            if min_page_count is not None:
                filters.append(Product.page_count >= min_page_count)
            if max_page_count is not None:
                filters.append(Product.page_count <= max_page_count)

            statement = (
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
            )
            return statement if python_metadata_filter else statement.limit(limit)

        def _execute(search_text: str | None) -> list[Product]:
            candidates = list(self.session.scalars(_build_statement(search_text)).all())
            if not python_metadata_filter:
                return candidates
            author_query = author.strip().casefold() if author else None
            publisher_query = publisher.strip().casefold() if publisher else None
            return [
                product
                for product in candidates
                if (
                    author_query is None
                    or any(
                        author_query in str(item).casefold() for item in product.authors
                    )
                )
                and (
                    publisher_query is None
                    or (
                        isinstance(product.publisher, str)
                        and publisher_query in product.publisher.casefold()
                    )
                )
            ][:limit]

        products = _execute(query)
        if not products and query:
            cleaned = _CATEGORY_PREFIX_PATTERN.sub("", query.strip()).strip()
            if cleaned and cleaned.casefold() != query.strip().casefold():
                products = _execute(cleaned)

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
        author: str | None = None,
        publisher: str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        min_rating: float | None = None,
    ) -> dict[str, object]:
        """Return cross-sectional facts from the historical Tiki Books snapshot."""

        filters = [Product.platform == "Tiki"]
        python_metadata_filter = self.session.get_bind().dialect.name == "sqlite" and (
            author is not None or publisher is not None
        )
        if python_metadata_filter:
            metadata_rows = self.session.execute(
                select(Product.id, Product.authors, Product.publisher).where(
                    Product.platform == "Tiki"
                )
            ).all()
            author_query = author.strip().casefold() if author else None
            publisher_query = publisher.strip().casefold() if publisher else None
            matching_ids = [
                product_id
                for product_id, authors, publisher_name in metadata_rows
                if (
                    author_query is None
                    or (
                        isinstance(authors, list)
                        and any(
                            author_query in str(item).casefold() for item in authors
                        )
                    )
                )
                and (
                    publisher_query is None
                    or (
                        isinstance(publisher_name, str)
                        and publisher_query in publisher_name.casefold()
                    )
                )
            ]
            filters.append(Product.id.in_(matching_ids))
        if category:
            filters.append(Product.category.ilike(category.strip()))
        if author and not python_metadata_filter:
            filters.append(cast(Product.authors, String).ilike(f"%{author.strip()}%"))
        if publisher and not python_metadata_filter:
            filters.append(Product.publisher.ilike(f"%{publisher.strip()}%"))
        if min_price is not None:
            filters.append(Product.price >= min_price)
        if max_price is not None:
            filters.append(Product.price <= max_price)
        if min_rating is not None:
            filters.append(Product.rating >= min_rating)

        totals = self.session.execute(
            select(
                func.count(Product.id),
                func.min(Product.price),
                func.max(Product.price),
                func.avg(Product.price),
                func.avg(Product.rating),
            ).where(*filters)
        ).one()
        category_rows = self.session.execute(
            select(Product.category, func.count(Product.id))
            .where(*filters)
            .group_by(Product.category)
            .order_by(Product.category)
        ).all()
        publisher_rows = self.session.execute(
            select(Product.publisher, func.count(Product.id))
            .where(*filters)
            .group_by(Product.publisher)
            .order_by(Product.publisher)
        ).all()
        book_rows = self.session.execute(
            select(Product.authors, Product.price, Product.rating).where(*filters)
        ).all()
        source_rows = self.session.execute(
            select(
                Product.source_id,
                DatasetSource.dataset_id,
                DatasetSource.dataset_version,
                DatasetSource.profile,
                DatasetSource.retrieved_at,
            )
            .outerjoin(DatasetSource, Product.source_id == DatasetSource.id)
            .where(*filters)
            .distinct()
            .order_by(Product.source_id)
        ).all()

        author_counts: Counter[str] = Counter()
        price_bands: Counter[str] = Counter()
        rating_bands: Counter[str] = Counter()
        for authors, price, rating in book_rows:
            normalized_authors = (
                [str(item).strip() for item in authors if str(item).strip()]
                if isinstance(authors, list)
                else []
            )
            author_counts.update(normalized_authors or ["Không rõ"])
            if int(price) < 100_000:
                price_bands["under_100k"] += 1
            elif int(price) < 200_000:
                price_bands["100k_to_under_200k"] += 1
            else:
                price_bands["200k_and_above"] += 1
            if rating is None:
                rating_bands["unknown"] += 1
            elif float(rating) < 4:
                rating_bands["under_4"] += 1
            elif float(rating) < 4.5:
                rating_bands["4_to_under_4_5"] += 1
            else:
                rating_bands["4_5_and_above"] += 1

        return {
            "filters": {
                "category": category.strip() if category else None,
                "author": author.strip() if author else None,
                "publisher": publisher.strip() if publisher else None,
                "min_price": min_price,
                "max_price": max_price,
                "min_rating": min_rating,
            },
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
            "category_distribution": {
                str(category_name): int(count) for category_name, count in category_rows
            },
            "author_distribution": dict(sorted(author_counts.items())),
            "publisher_distribution": {
                str(publisher_name or "Không rõ"): int(count)
                for publisher_name, count in publisher_rows
            },
            "price_distribution": dict(sorted(price_bands.items())),
            "rating_distribution": dict(sorted(rating_bands.items())),
            "analysis_type": "cross_sectional_snapshot",
            "snapshot_sources": [
                {
                    "source_id": source_id,
                    "dataset_id": dataset_id,
                    "dataset_version": dataset_version,
                    "profile": dataset_profile,
                    "retrieved_at": (
                        retrieved_at.isoformat() if retrieved_at is not None else None
                    ),
                }
                for (
                    source_id,
                    dataset_id,
                    dataset_version,
                    dataset_profile,
                    retrieved_at,
                ) in source_rows
            ],
            "provenance": [
                _source_provenance(
                    source_id=source_id,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    dataset_profile=dataset_profile,
                    fields=("category", "authors", "publisher", "price", "rating"),
                )
                for (
                    source_id,
                    dataset_id,
                    dataset_version,
                    dataset_profile,
                    _,
                ) in source_rows
            ],
        }


def _product_summary(product: Product) -> dict[str, object]:
    """Serialize a Product ORM object into a JSON-friendly fact record."""

    return {
        "id": product.id,
        "name": product.name,
        "authors": list(product.authors),
        "publisher": product.publisher,
        "category": product.category,
        "page_count": product.page_count,
        "price": product.price,
        "original_price": product.original_price,
        "rating": product.rating,
        "sold_count": product.sold_count,
        "source_popularity": product.sold_count,
        "source_review_count": product.source_review_count,
        "cover_url": product.cover_url,
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
    fields: tuple[str, ...] = (
        "name",
        "authors",
        "publisher",
        "category",
        "page_count",
        "price",
        "rating",
        "sold_count",
    ),
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
