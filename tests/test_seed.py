from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import reset_legacy_seed as legacy_reset
from app.db import seed
from app.db import session as db_session
from app.db.base import Base
from app.models.dataset_source import DatasetSource
from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_SNAPSHOT = PROJECT_ROOT / "data" / "snapshots" / "tiki-books-v4-test"
EVAL_SNAPSHOT = PROJECT_ROOT / "data" / "snapshots" / "tiki-books-v4-eval"


def test_autouse_database_uses_clean_tiki_books_snapshot() -> None:
    with db_session.session_scope() as session:
        source = session.scalar(select(DatasetSource))
        assert source is not None
        assert source.dataset_id == "tiki-books"
        assert source.profile == "test"
        assert source.quality_report_sha256 is not None
        assert session.scalar(select(func.count()).select_from(Shop)) == 0
        assert session.scalar(select(func.count()).select_from(Product)) == 24
        assert session.scalar(select(func.count()).select_from(Review)) == 115
        assert (
            session.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.platform != "Tiki")
            )
            == 0
        )


def test_snapshot_bootstrap_is_idempotent() -> None:
    counts = seed.seed_database(
        snapshot_dir=TEST_SNAPSHOT,
        session_factory=db_session.SessionLocal,
    )

    assert counts == {"source_id": 1, "products": 24, "reviews": 115}


def test_snapshot_bootstrap_uses_configured_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seed.settings, "public_snapshot_dir", TEST_SNAPSHOT)

    counts = seed.seed_database(session_factory=db_session.SessionLocal)

    assert counts["products"] == 24
    assert counts["reviews"] == 115


def test_snapshot_bootstrap_refuses_mixed_rows_without_deleting_them() -> None:
    with db_session.SessionLocal.begin() as session:
        session.add(
            Shop(
                name="Dữ liệu không thuộc snapshot",
                platform="Tiki",
                rating=5,
            )
        )

    with pytest.raises(seed.SeedConflictError, match="mixed or unknown"):
        seed.seed_database(
            snapshot_dir=TEST_SNAPSHOT,
            session_factory=db_session.SessionLocal,
        )

    with db_session.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Shop)) == 1
        assert session.scalar(select(func.count()).select_from(Product)) == 24
        assert session.scalar(select(func.count()).select_from(Review)) == 115


def test_snapshot_bootstrap_refuses_a_different_profile() -> None:
    with pytest.raises(seed.SeedConflictError, match="different snapshot"):
        seed.seed_database(
            snapshot_dir=EVAL_SNAPSHOT,
            session_factory=db_session.SessionLocal,
        )

    with db_session.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(DatasetSource)) == 1
        assert session.scalar(select(func.count()).select_from(Product)) == 24


def test_legacy_reset_refuses_current_snapshot_and_requires_confirmation() -> None:
    with pytest.raises(
        legacy_reset.LegacySeedResetError,
        match="explicit disposable",
    ):
        legacy_reset.reset_legacy_seed(
            session_factory=db_session.SessionLocal,
        )

    with pytest.raises(
        legacy_reset.LegacySeedResetError,
        match="mixed, empty, or unknown",
    ):
        legacy_reset.reset_legacy_seed(
            session_factory=db_session.SessionLocal,
            confirm_disposable=True,
        )

    with db_session.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(DatasetSource)) == 1
        assert session.scalar(select(func.count()).select_from(Product)) == 24


def test_legacy_reset_is_blocked_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_reset.settings, "app_env", "production")
    with pytest.raises(legacy_reset.LegacySeedResetError, match="production"):
        legacy_reset.reset_legacy_seed(
            session_factory=db_session.SessionLocal,
            confirm_disposable=True,
        )


def test_legacy_reset_deletes_only_an_exact_guarded_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine, factory = _guarded_fixture_database(tmp_path / "exact.db")
    try:
        with factory() as session:
            fingerprint = legacy_reset._legacy_seed_fingerprint(session)
        monkeypatch.setattr(
            legacy_reset,
            "_LEGACY_SEED_COUNTS",
            {"dataset_sources": 0, "shops": 1, "products": 1, "reviews": 1},
        )
        monkeypatch.setattr(
            legacy_reset,
            "_LEGACY_SEED_FINGERPRINT",
            fingerprint,
        )

        removed = legacy_reset.reset_legacy_seed(
            session_factory=factory,
            confirm_disposable=True,
        )

        assert removed == {"shops": 1, "products": 1, "reviews": 1}
        with factory() as session:
            assert legacy_reset._row_counts(session) == {
                "dataset_sources": 0,
                "shops": 0,
                "products": 0,
                "reviews": 0,
            }
    finally:
        engine.dispose()


def test_legacy_reset_refuses_same_counts_with_a_different_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine, factory = _guarded_fixture_database(tmp_path / "changed.db")
    try:
        monkeypatch.setattr(
            legacy_reset,
            "_LEGACY_SEED_COUNTS",
            {"dataset_sources": 0, "shops": 1, "products": 1, "reviews": 1},
        )
        monkeypatch.setattr(legacy_reset, "_LEGACY_SEED_FINGERPRINT", "0" * 64)

        with pytest.raises(
            legacy_reset.LegacySeedResetError,
            match="fingerprint",
        ):
            legacy_reset.reset_legacy_seed(
                session_factory=factory,
                confirm_disposable=True,
            )

        with factory() as session:
            assert legacy_reset._row_counts(session) == {
                "dataset_sources": 0,
                "shops": 1,
                "products": 1,
                "reviews": 1,
            }
    finally:
        engine.dispose()


def test_legacy_fingerprint_tracks_every_current_model_column() -> None:
    with db_session.SessionLocal() as session:
        legacy_reset._verify_fingerprint_schema(session)
    assert len(legacy_reset._LEGACY_SEED_FINGERPRINT) == 64


def test_seed_cli_has_no_destructive_reset_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "reset" not in inspect.signature(seed.seed_database).parameters
    monkeypatch.setattr(sys, "argv", ["ecommerce-seed", "--reset"])

    with pytest.raises(SystemExit) as exc_info:
        seed.main()

    assert exc_info.value.code == 2


def _guarded_fixture_database(
    database_path: Path,
) -> tuple[object, sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    Base.metadata.create_all(engine)
    with factory.begin() as session:
        shop = Shop(
            name="Nhà sách kiểm thử",
            platform="Tiki",
            rating=5,
        )
        product = Product(
            shop=shop,
            name="Sách kiểm thử guard",
            category="Sách",
            price=100_000,
            original_price=None,
            rating=5,
            sold_count=None,
            description="Bản ghi tối thiểu để kiểm tra cơ chế bảo vệ.",
            platform="Tiki",
        )
        review = Review(
            product=product,
            rating=5,
            content="Nội dung kiểm thử.",
        )
        session.add_all((shop, product, review))
    return engine, factory
