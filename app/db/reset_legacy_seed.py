"""Guarded removal of the retired hard-coded sample dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db import session as db_session
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop

_LEGACY_SEED_COUNTS = {
    "dataset_sources": 0,
    "shops": 5,
    "products": 30,
    "reviews": 150,
}
_LEGACY_SEED_FINGERPRINT = (
    "8e857175a594d3cba5e881f064e125a5597680d18934a3b83f51d350c128ae4b"
)
_FINGERPRINT_VERSION = b"legacy-synthetic-seed-v1\n"
_EXPECTED_COLUMNS = {
    "shops": (
        "id",
        "name",
        "platform",
        "rating",
        "created_at",
    ),
    "products": (
        "id",
        "source_id",
        "external_id",
        "shop_id",
        "name",
        "authors",
        "publisher",
        "category",
        "page_count",
        "price",
        "original_price",
        "rating",
        "sold_count",
        "source_review_count",
        "cover_url",
        "description",
        "platform",
        "seller_name",
        "source_metadata",
        "created_at",
        "updated_at",
    ),
    "reviews": (
        "id",
        "source_id",
        "external_id",
        "product_id",
        "rating",
        "title",
        "content",
        "helpful_count",
        "created_at",
    ),
}


class _Digest(Protocol):
    def update(self, value: bytes, /) -> None: ...


class LegacySeedResetError(RuntimeError):
    """Raised when a database is not the exact retired disposable dataset."""


def reset_legacy_seed(
    *,
    session_factory: sessionmaker[Session] | None = None,
    confirm_disposable: bool = False,
) -> dict[str, int]:
    """Delete only the exact retired seed after an explicit disposable guard."""

    if not confirm_disposable:
        raise LegacySeedResetError(
            "legacy reset requires explicit disposable-database confirmation"
        )
    if settings.app_env == "production":
        raise LegacySeedResetError("legacy reset is never allowed in production")

    target_factory = session_factory or db_session.SessionLocal
    with target_factory.begin() as session:
        _lock_legacy_tables(session)
        _verify_fingerprint_schema(session)
        counts = _row_counts(session)
        if counts != _LEGACY_SEED_COUNTS:
            raise LegacySeedResetError(
                "database is mixed, empty, or unknown; legacy reset refused"
            )
        if _legacy_seed_fingerprint(session) != _LEGACY_SEED_FINGERPRINT:
            raise LegacySeedResetError(
                "database does not match the retired seed fingerprint"
            )

        deleted_reviews = _affected_rows(session.execute(delete(Review)))
        deleted_products = _affected_rows(session.execute(delete(Product)))
        deleted_shops = _affected_rows(session.execute(delete(Shop)))
        if (
            deleted_reviews != counts["reviews"]
            or deleted_products != counts["products"]
            or deleted_shops != counts["shops"]
        ):
            raise LegacySeedResetError("legacy delete counts did not match the guard")
        if any(_row_counts(session).values()):
            raise LegacySeedResetError("legacy rows were not removed atomically")

    return {key: value for key, value in counts.items() if key != "dataset_sources"}


def _affected_rows(result: object) -> int:
    rowcount = getattr(result, "rowcount", None)
    if not isinstance(rowcount, int):
        raise LegacySeedResetError("database did not report legacy delete counts")
    return rowcount


def _lock_legacy_tables(session: Session) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
        return
    if dialect == "postgresql":
        session.execute(
            text(
                "LOCK TABLE dataset_sources, reviews, products, shops "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
        return
    raise LegacySeedResetError(
        f"legacy reset does not support the {dialect!r} database dialect"
    )


def _verify_fingerprint_schema(session: Session) -> None:
    model_columns = {
        "shops": tuple(Shop.__table__.columns.keys()),
        "products": tuple(Product.__table__.columns.keys()),
        "reviews": tuple(Review.__table__.columns.keys()),
    }
    inspector = inspect(session.get_bind())
    database_columns = {
        table_name: tuple(
            str(column["name"]) for column in inspector.get_columns(table_name)
        )
        for table_name in _EXPECTED_COLUMNS
    }
    if model_columns != _EXPECTED_COLUMNS or database_columns != _EXPECTED_COLUMNS:
        raise LegacySeedResetError(
            "database model changed; legacy reset fingerprint is no longer safe"
        )


def _row_counts(session: Session) -> dict[str, int]:
    return {
        "dataset_sources": session.scalar(
            select(func.count()).select_from(DatasetSource)
        )
        or 0,
        "shops": session.scalar(select(func.count()).select_from(Shop)) or 0,
        "products": session.scalar(select(func.count()).select_from(Product)) or 0,
        "reviews": session.scalar(select(func.count()).select_from(Review)) or 0,
    }


def _legacy_seed_fingerprint(session: Session) -> str:
    digest = hashlib.sha256(_FINGERPRINT_VERSION)
    _update_fingerprint(
        digest,
        "shops",
        session.execute(
            select(
                Shop.id,
                Shop.name,
                Shop.platform,
                Shop.rating,
                Shop.created_at,
            ).order_by(Shop.id)
        ).tuples(),
    )
    _update_fingerprint(
        digest,
        "products",
        session.execute(
            select(
                Product.id,
                Product.source_id,
                Product.external_id,
                Product.shop_id,
                Product.name,
                Product.authors,
                Product.publisher,
                Product.category,
                Product.page_count,
                Product.price,
                Product.original_price,
                Product.rating,
                Product.sold_count,
                Product.source_review_count,
                Product.cover_url,
                Product.description,
                Product.platform,
                Product.seller_name,
                Product.source_metadata,
                Product.created_at,
                Product.updated_at,
            ).order_by(Product.id)
        ).tuples(),
    )
    _update_fingerprint(
        digest,
        "reviews",
        session.execute(
            select(
                Review.id,
                Review.source_id,
                Review.external_id,
                Review.product_id,
                Review.rating,
                Review.title,
                Review.content,
                Review.helpful_count,
                Review.created_at,
            ).order_by(Review.id)
        ).tuples(),
    )
    return digest.hexdigest()


def _update_fingerprint(
    digest: _Digest,
    table_name: str,
    rows: Iterable[tuple[object, ...]],
) -> None:
    digest.update(table_name.encode("ascii") + b"\n")
    for row in rows:
        payload = [_canonical_value(value) for value in row]
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove only the exact retired seed from a disposable database.",
    )
    parser.add_argument(
        "--confirm-disposable",
        metavar="DELETE-LEGACY-SEED",
        help="type DELETE-LEGACY-SEED to confirm the disposable operation",
    )
    arguments = parser.parse_args()
    try:
        removed = reset_legacy_seed(
            confirm_disposable=(arguments.confirm_disposable == "DELETE-LEGACY-SEED"),
        )
    except LegacySeedResetError as exc:
        parser.error(str(exc))
    print("Retired seed removed")
    print(f"Shops: {removed['shops']}")
    print(f"Products: {removed['products']}")
    print(f"Reviews: {removed['reviews']}")


if __name__ == "__main__":
    main()
