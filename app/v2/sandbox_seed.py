"""Insert-only seeding for the isolated v2 demo offer catalog."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TypeAlias, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from app.models.product import Product
from app.models.v2 import V2Offer
from app.v2.authorization import DEMO_STORE_ID
from app.v2.tools import CatalogSnapshot, load_catalog_snapshot

SessionFactory: TypeAlias = Callable[[], Session]


@dataclass(frozen=True, slots=True)
class SandboxSeedReport:
    """Counts and source identity returned by one seed transaction."""

    tenant_id: str
    store_id: str
    catalog_fingerprint: str
    inserted: int
    existing: int
    skipped_zero_price: int

    @property
    def source_fingerprint(self) -> str:
        """Alias for callers that describe the catalog identity as a source."""

        return self.catalog_fingerprint

    @property
    def catalog_version_id(self) -> str:
        """The existing catalog utility's version-id terminology."""

        return self.catalog_fingerprint

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


def seed_demo_offers(
    session_factory: SessionFactory,
    *,
    tenant_id: str,
    store_id: str = DEMO_STORE_ID,
) -> SandboxSeedReport:
    """Insert one demo offer for each eligible positive-price catalog product.

    The caller supplies a session factory explicitly. Existing offers are only
    observed, never updated, reactivated, deleted, or reset.
    """

    _validate_scope(tenant_id=tenant_id, store_id=store_id)
    catalog_snapshot = load_catalog_snapshot(session_factory)

    with session_factory() as session:
        try:
            products = _eligible_products(session, catalog_snapshot)
            existing_product_ids = _existing_product_ids(
                session,
                tenant_id=tenant_id,
                store_id=store_id,
            )
            inserted = 0
            existing = 0
            skipped_zero_price = 0

            for product in products:
                if product.price <= 0:
                    skipped_zero_price += 1
                    continue
                if product.id in existing_product_ids:
                    existing += 1
                    continue

                offer = _offer_values(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    product=product,
                )
                if _insert_if_missing(session, offer):
                    inserted += 1
                    existing_product_ids.add(product.id)
                else:
                    # A concurrent seed may have inserted the same product
                    # after the initial read. The conflict leaves that row
                    # untouched and is reported as existing.
                    existing += 1

            session.commit()
        except Exception:
            session.rollback()
            raise

    return SandboxSeedReport(
        tenant_id=tenant_id,
        store_id=store_id,
        catalog_fingerprint=catalog_snapshot.version_id,
        inserted=inserted,
        existing=existing,
        skipped_zero_price=skipped_zero_price,
    )


def _validate_scope(*, tenant_id: str, store_id: str) -> None:
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 160:
        raise ValueError(
            "tenant_id must be a non-empty string of at most 160 characters"
        )
    if store_id != DEMO_STORE_ID:
        raise ValueError("sandbox seeding requires store_id='demo'")


def _eligible_products(
    session: Session,
    catalog_snapshot: CatalogSnapshot,
) -> tuple[Product, ...]:
    return tuple(
        session.scalars(
            select(Product)
            .where(
                Product.platform == "Tiki",
                Product.source_id.in_(catalog_snapshot.source_ids),
            )
            .order_by(Product.id)
        )
    )


def _existing_product_ids(
    session: Session,
    *,
    tenant_id: str,
    store_id: str,
) -> set[int]:
    return set(
        session.scalars(
            select(V2Offer.product_id).where(
                V2Offer.tenant_id == tenant_id,
                V2Offer.store_id == store_id,
            )
        )
    )


def _offer_values(
    *,
    tenant_id: str,
    store_id: str,
    product: Product,
) -> dict[str, int | str | bool]:
    return {
        "id": _stable_offer_id(
            tenant_id=tenant_id,
            store_id=store_id,
            product_id=product.id,
        ),
        "tenant_id": tenant_id,
        "store_id": store_id,
        "product_id": product.id,
        "demo_price_vnd": product.price,
        "stock": 10,
        "version": 1,
        "is_active": True,
    }


def _stable_offer_id(*, tenant_id: str, store_id: str, product_id: int) -> str:
    canonical = json.dumps(
        {
            "namespace": "v2-demo-offer",
            "tenant_id": tenant_id,
            "store_id": store_id,
            "product_id": product_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # V2Offer.id is String(64): keep the readable namespace and fit the full
    # digest without making the ID depend on mutable offer fields.
    return f"offer_{hashlib.sha256(canonical).hexdigest()[:58]}"


def _insert_if_missing(
    session: Session,
    values: dict[str, int | str | bool],
) -> bool:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return _execute_insert(
            session,
            postgres_insert(V2Offer)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[
                    V2Offer.tenant_id,
                    V2Offer.store_id,
                    V2Offer.product_id,
                ]
            )
            .returning(V2Offer.product_id),
        )
    if dialect == "sqlite":
        return _execute_insert(
            session,
            sqlite_insert(V2Offer)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[
                    V2Offer.tenant_id,
                    V2Offer.store_id,
                    V2Offer.product_id,
                ]
            )
            .returning(V2Offer.product_id),
        )
    return _insert_with_savepoint(session, values)


def _execute_insert(session: Session, statement: Insert) -> bool:
    result = cast(CursorResult[int], session.execute(statement))
    return result.scalar_one_or_none() is not None


def _insert_with_savepoint(
    session: Session,
    values: dict[str, int | str | bool],
) -> bool:
    offer = V2Offer(**values)
    try:
        with session.begin_nested():
            session.add(offer)
            session.flush()
    except IntegrityError:
        return False
    return True
