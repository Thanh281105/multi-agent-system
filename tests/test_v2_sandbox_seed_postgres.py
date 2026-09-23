"""Real PostgreSQL checks for Package 5 sandbox guards and offer seeding."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.migrate import _migration_config, upgrade_database
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.v2 import (
    V2ActionAudit,
    V2Cart,
    V2Conversation,
    V2Offer,
    V2Proposal,
    V2Turn,
)
from app.v2.sandbox_seed import SandboxSeedReport, seed_demo_offers
from tests.v2_postgres_support import disposable_postgres_database

OBSERVED_AT = datetime(2026, 8, 30, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def postgres_seed_database() -> Iterator[tuple[str, sessionmaker[Session]]]:
    with disposable_postgres_database("thanh_v2_p2_seed_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with sessions() as session:
                _seed_historical_catalog(session)
            yield database_url, sessions
        finally:
            engine.dispose()


def _seed_historical_catalog(session: Session) -> None:
    source = DatasetSource(
        id=1,
        dataset_id="v2-sandbox-fixture",
        dataset_version="2026-08-30",
        profile="test",
        source_url="https://example.test/synthetic-books",
        source_license="Synthetic test fixture",
        source_revision=1,
        raw_archive_sha256="a" * 64,
        snapshot_sha256="b" * 64,
        products_sha256="c" * 64,
        reviews_sha256="d" * 64,
        sampling_seed=1,
        product_count=4,
        review_count=0,
        retrieved_at=OBSERVED_AT,
    )
    session.add(source)
    session.flush()
    session.add_all(
        [
            Product(
                id=1,
                source_id=source.id,
                external_id="synthetic-1",
                name="Sách thử nghiệm 1",
                authors=["Tác giả một"],
                publisher="Nhà xuất bản mẫu",
                category="Công nghệ",
                page_count=100,
                price=100_000,
                rating=4.1,
                sold_count=10,
                source_review_count=0,
                description="Synthetic book description.",
                platform="Tiki",
            ),
            Product(
                id=2,
                source_id=source.id,
                external_id="synthetic-2",
                name="Sách thử nghiệm 2",
                authors=["Tác giả hai"],
                publisher="Nhà xuất bản mẫu",
                category="Công nghệ",
                page_count=120,
                price=0,
                rating=4.2,
                sold_count=20,
                source_review_count=0,
                description="Synthetic book description.",
                platform="Tiki",
            ),
            Product(
                id=3,
                source_id=source.id,
                external_id="synthetic-3",
                name="Sách thử nghiệm 3",
                authors=["Tác giả ba"],
                publisher="Nhà xuất bản mẫu",
                category="Văn học",
                page_count=140,
                price=250_000,
                rating=4.3,
                sold_count=30,
                source_review_count=0,
                description="Synthetic book description.",
                platform="Tiki",
            ),
            Product(
                id=4,
                source_id=source.id,
                external_id="synthetic-4",
                name="Non Tiki product",
                authors=["Tác giả khác"],
                publisher="Nhà xuất bản mẫu",
                category="Văn học",
                page_count=160,
                price=300_000,
                rating=4.4,
                sold_count=40,
                source_review_count=0,
                description="Excluded platform fixture.",
                platform="Shopee",
            ),
        ]
    )
    session.commit()


def test_seed_is_deterministic_insert_only_and_reports_catalog_identity(
    postgres_seed_database: tuple[str, sessionmaker[Session]],
) -> None:
    _, sessions = postgres_seed_database

    first = seed_demo_offers(sessions, tenant_id="tenant-seed")
    second = seed_demo_offers(sessions, tenant_id="tenant-seed")

    assert first == SandboxSeedReport(
        tenant_id="tenant-seed",
        store_id="demo",
        catalog_fingerprint=first.catalog_fingerprint,
        inserted=2,
        existing=0,
        skipped_zero_price=1,
    )
    assert second.catalog_fingerprint == first.catalog_fingerprint
    assert second.inserted == 0
    assert second.existing == 2
    assert second.skipped_zero_price == 1

    with sessions() as session:
        offers = tuple(
            session.scalars(
                select(V2Offer)
                .where(V2Offer.tenant_id == "tenant-seed")
                .order_by(V2Offer.product_id)
            )
        )
    assert [offer.product_id for offer in offers] == [1, 3]
    assert all(offer.stock == 10 for offer in offers)
    assert all(offer.version == 1 for offer in offers)
    assert all(offer.is_active for offer in offers)


def test_seed_never_resets_merchant_mutations(
    postgres_seed_database: tuple[str, sessionmaker[Session]],
) -> None:
    _, sessions = postgres_seed_database
    seed_demo_offers(sessions, tenant_id="tenant-mutated")

    with sessions() as session:
        offer = session.scalar(
            select(V2Offer).where(
                V2Offer.tenant_id == "tenant-mutated",
                V2Offer.product_id == 1,
            )
        )
        assert offer is not None
        offer.demo_price_vnd = 42_000
        offer.stock = 3
        offer.version = 9
        offer.is_active = False
        session.commit()

    report = seed_demo_offers(sessions, tenant_id="tenant-mutated")
    assert report.inserted == 0
    assert report.existing == 2
    assert report.skipped_zero_price == 1

    with sessions() as session:
        offer = session.scalar(
            select(V2Offer).where(
                V2Offer.tenant_id == "tenant-mutated",
                V2Offer.product_id == 1,
            )
        )
        product = session.get(Product, 1)
        assert offer is not None
        assert (offer.demo_price_vnd, offer.stock, offer.version, offer.is_active) == (
            42_000,
            3,
            9,
            False,
        )
        assert product is not None
        assert product.price == 100_000


def test_concurrent_seeds_leave_one_offer_per_product(
    postgres_seed_database: tuple[str, sessionmaker[Session]],
) -> None:
    _, sessions = postgres_seed_database

    def run_seed() -> SandboxSeedReport:
        return seed_demo_offers(sessions, tenant_id="tenant-concurrent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(lambda _: run_seed(), range(2)))

    assert sum(report.inserted for report in reports) == 2
    assert sum(report.existing for report in reports) == 2
    assert all(report.skipped_zero_price == 1 for report in reports)
    with sessions() as session:
        assert (
            session.scalar(
                text(
                    "SELECT COUNT(*) FROM v2_offers "
                    "WHERE tenant_id = 'tenant-concurrent'"
                )
            )
            == 2
        )
        assert (
            session.scalar(
                text(
                    "SELECT COUNT(DISTINCT product_id) FROM v2_offers "
                    "WHERE tenant_id = 'tenant-concurrent'"
                )
            )
            == 2
        )


def test_postgres_partial_cart_index_and_immutable_audit_trigger(
    postgres_seed_database: tuple[str, sessionmaker[Session]],
) -> None:
    database_url, sessions = postgres_seed_database
    engine = create_engine(database_url)
    try:
        indexes = inspect(engine).get_indexes("v2_carts")
        active_index = next(
            index for index in indexes if index["name"] == "uq_v2_cart_active_owner"
        )
        assert active_index["unique"] is True
        with engine.connect() as connection:
            index_definition = connection.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_v2_cart_active_owner'"
                )
            )
            index_predicate = connection.scalar(
                text(
                    "SELECT pg_get_expr(indexes.indpred, indexes.indrelid) "
                    "FROM pg_index AS indexes "
                    "JOIN pg_class AS relation ON relation.oid = indexes.indexrelid "
                    "WHERE relation.relname = 'uq_v2_cart_active_owner'"
                )
            )
            trigger_names = set(
                connection.scalars(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE tgrelid = 'v2_action_audit'::regclass "
                        "AND NOT tgisinternal"
                    )
                )
            )
        assert index_definition is not None
        normalized_definition = " ".join(str(index_definition).lower().split())
        assert "unique index" in normalized_definition
        assert "tenant_id, principal_id, store_id" in normalized_definition
        assert index_predicate is not None
        normalized_predicate = "".join(str(index_predicate).lower().split())
        assert "status" in normalized_predicate
        assert "=" in normalized_predicate
        assert "active" in normalized_predicate
        assert "trg_v2_action_audit_immutable" in trigger_names

        with sessions() as session:
            session.add(
                V2Cart(
                    id="cart-active",
                    tenant_id="tenant-cart",
                    principal_id="principal-cart",
                    store_id="demo",
                    status="active",
                )
            )
            session.commit()
            session.add(
                V2Cart(
                    id="cart-checked-out",
                    tenant_id="tenant-cart",
                    principal_id="principal-cart",
                    store_id="demo",
                    status="checked_out",
                )
            )
            session.commit()
            session.add(
                V2Cart(
                    id="cart-active-duplicate",
                    tenant_id="tenant-cart",
                    principal_id="principal-cart",
                    store_id="demo",
                    status="active",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        _seed_audit_row(sessions)
        with sessions() as session:
            audit = session.get(V2ActionAudit, "audit-1")
            assert audit is not None
            audit.event_type = "rewritten"
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            session.delete(audit)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            assert session.get(V2ActionAudit, "audit-1") is not None
    finally:
        engine.dispose()


def _seed_audit_row(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        conversation = V2Conversation(
            id="conversation-audit",
            tenant_id="tenant-audit",
            principal_id="principal-audit",
            mode="shopper",
            store_id="demo",
        )
        turn = V2Turn(
            id="turn-audit",
            conversation_id=conversation.id,
            tenant_id=conversation.tenant_id,
            principal_id=conversation.principal_id,
            mode=conversation.mode,
            store_id=conversation.store_id,
            client_turn_id="client-audit",
            request_payload_hash="a" * 64,
            request_payload={},
            execution_state="pending",
            runtime_metadata={},
        )
        proposal = V2Proposal(
            id="proposal-audit",
            tenant_id=conversation.tenant_id,
            principal_id=conversation.principal_id,
            mode=conversation.mode,
            store_id=conversation.store_id,
            conversation_id=conversation.id,
            turn_id=turn.id,
            action_type="cart.inspect",
            target_type="cart",
            target_id="cart-active",
            target_version=1,
            before_payload={},
            after_payload={},
            proposal_version=1,
            status="proposed",
            expires_at=OBSERVED_AT + timedelta(days=1),
        )
        session.add(conversation)
        session.flush()
        session.add(turn)
        session.flush()
        session.add(proposal)
        session.flush()
        session.add(
            V2ActionAudit(
                id="audit-1",
                tenant_id=conversation.tenant_id,
                principal_id=conversation.principal_id,
                mode=conversation.mode,
                store_id=conversation.store_id,
                proposal_id=proposal.id,
                event_type="created",
                payload={},
            )
        )
        session.commit()


def test_migration_0008_supports_runtime_upgrade_and_reapply() -> None:
    with disposable_postgres_database("thanh_v2_p2_guard_") as database_url:
        upgrade_database(database_url, revision="20260909_0007")
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "20260909_0007"
                )

            upgrade_database(database_url)
            migration_config = _migration_config()
            with engine.begin() as connection:
                migration_config.attributes["connection"] = connection
                command.downgrade(migration_config, "20260909_0007")
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "20260909_0007"
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT COUNT(*) FROM pg_indexes "
                            "WHERE indexname = 'uq_v2_cart_active_owner'"
                        )
                    )
                    == 0
                )

            upgrade_database(database_url)
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == "20260910_0008"
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT COUNT(*) FROM pg_indexes "
                            "WHERE indexname = 'uq_v2_cart_active_owner'"
                        )
                    )
                    == 1
                )
        finally:
            engine.dispose()
