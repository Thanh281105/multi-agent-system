"""Exact accounting and PostgreSQL race tests for the provider ledger."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.migrate import upgrade_database
from app.models.budget import (
    ProviderAttempt,
    ProviderBudgetAccount,
    ProviderBudgetScope,
)
from app.shared.budget import (
    BudgetAttemptLimitError,
    BudgetConcurrencyError,
    BudgetConflictError,
    BudgetDeadlineError,
    BudgetLimitExceededError,
    PricingManifest,
    PricingManifestError,
    ProviderUsage,
    SQLProviderBudgetLedger,
    default_pricing_manifest_path,
)
from tests.v2_postgres_support import disposable_postgres_database


@pytest.fixture
def ledger_store(
    tmp_path: Path,
) -> Iterator[tuple[SQLProviderBudgetLedger, sessionmaker[Session]]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'budget.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    ledger = SQLProviderBudgetLedger(
        factory,
        PricingManifest.load(default_pricing_manifest_path()),
    )
    try:
        yield ledger, factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _create_scope(
    ledger: SQLProviderBudgetLedger,
    *,
    account_id: str = "shared",
    scope_id: str = "turn-1",
    purpose: str = "chat",
    hard_limit: int = 250_000_000,
) -> None:
    ledger.create_account(account_id=account_id)
    ledger.create_scope(
        scope_id=scope_id,
        account_id=account_id,
        purpose=purpose,  # type: ignore[arg-type]
        hard_limit_nano_usd=hard_limit,
    )


def _reserve(
    ledger: SQLProviderBudgetLedger,
    *,
    scope_id: str = "turn-1",
    call_id: str = "mcall_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    attempt_number: int = 1,
    purpose: str = "chat",
    input_bound: int = 100,
    output_bound: int = 10,
):
    return ledger.reserve_attempt(
        scope_id=scope_id,
        call_id=call_id,
        attempt_number=attempt_number,
        operation="generation",
        purpose=purpose,  # type: ignore[arg-type]
        model="gpt-5.4-mini",
        request_fingerprint_sha256="a" * 64,
        input_token_bound=input_bound,
        output_token_bound=output_bound,
    )


def test_manifest_uses_exact_prices_and_canonical_cross_platform_hash(
    tmp_path: Path,
) -> None:
    source = default_pricing_manifest_path()
    document = json.loads(source.read_text(encoding="utf-8"))
    compact = tmp_path / "compact.json"
    windows = tmp_path / "windows.json"
    compact.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    windows.write_bytes(
        json.dumps(document, ensure_ascii=False, indent=4)
        .replace("\n", "\r\n")
        .encode()
    )

    manifest = PricingManifest.load(source)
    assert manifest.manifest_sha256 == PricingManifest.load(compact).manifest_sha256
    assert manifest.manifest_sha256 == PricingManifest.load(windows).manifest_sha256
    mini = manifest.resolve("gpt-5.4-mini", "generation")
    nano = manifest.resolve("gpt-5.4-nano", "generation")
    embedding = manifest.resolve("text-embedding-3-small", "embedding")
    assert (mini.input_nano_usd_per_token, mini.output_nano_usd_per_token) == (
        750,
        4_500,
    )
    assert (nano.input_nano_usd_per_token, nano.output_nano_usd_per_token) == (
        200,
        1_250,
    )
    assert embedding.input_nano_usd_per_token == 20
    with pytest.raises(PricingManifestError, match="not_priced"):
        manifest.resolve("unknown-model", "generation")


def test_known_settlement_moves_exact_cost_and_keeps_retry_usage(
    ledger_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session]],
) -> None:
    ledger, _ = ledger_store
    _create_scope(ledger)
    reservation = _reserve(ledger)
    assert reservation.quote.reserved_nano_usd == 120_000
    assert ledger.mark_dispatched(scope_id="turn-1", attempt_id=reservation.attempt_id)
    usage = ProviderUsage(
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=10,
        reasoning_tokens=3,
        total_tokens=110,
    )
    settlement = {
        "scope_id": "turn-1",
        "attempt_id": reservation.attempt_id,
        "usage": usage,
        "response_id": "resp_1",
        "response_model": "gpt-5.4-mini-2026-03-17",
        "response_service_tier": "default",
    }
    ledger.settle_known_usage(**settlement)
    ledger.settle_known_usage(**settlement)
    ledger.record_attempt_result(
        scope_id="turn-1",
        attempt_id=reservation.attempt_id,
        result_status="success",
    )
    ledger.record_attempt_result(
        scope_id="turn-1",
        attempt_id=reservation.attempt_id,
        result_status="success",
    )

    summary = ledger.scope_usage_summary("turn-1")
    assert summary.costs.known_nano_usd == 106_500
    assert summary.costs.reserved_nano_usd == 0
    assert summary.costs.unknown_nano_usd == 0
    assert summary.reasoning_tokens == 3
    assert summary.total_tokens == 110
    with pytest.raises(BudgetConflictError, match="known_settlement_conflict"):
        ledger.settle_known_usage(**{**settlement, "response_id": "resp_different"})


def test_unknown_settlement_retains_capacity_and_duplicate_never_dispatches(
    ledger_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session]],
) -> None:
    ledger, _ = ledger_store
    reservation_cost = 120_000
    _create_scope(ledger, hard_limit=reservation_cost)
    reservation = _reserve(ledger)
    duplicate = _reserve(ledger)
    assert duplicate.attempt_id == reservation.attempt_id
    assert duplicate.dispatch_allowed is False
    assert duplicate.duplicate is True
    assert ledger.mark_dispatched(scope_id="turn-1", attempt_id=reservation.attempt_id)
    ledger.settle_unknown_usage(
        scope_id="turn-1",
        attempt_id=reservation.attempt_id,
        result_status="timeout",
        error_code="model_timeout",
    )

    summary = ledger.scope_summary("turn-1")
    assert summary.known_nano_usd == 0
    assert summary.reserved_nano_usd == 0
    assert summary.unknown_nano_usd == reservation_cost
    assert summary.encumbered_nano_usd == reservation_cost
    with pytest.raises(BudgetLimitExceededError, match="scope_budget"):
        _reserve(
            ledger,
            call_id="mcall_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )


def test_central_limits_purpose_and_pricing_identity_fail_closed(
    ledger_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session]],
) -> None:
    ledger, _ = ledger_store
    _create_scope(ledger)
    with pytest.raises(BudgetConflictError, match="purpose_scope_mismatch"):
        _reserve(ledger, purpose="judge")
    with pytest.raises(BudgetLimitExceededError, match="payload_limit"):
        _reserve(ledger, input_bound=12_001)

    reservation = _reserve(ledger)
    ledger.mark_dispatched(scope_id="turn-1", attempt_id=reservation.attempt_id)
    usage = ProviderUsage(
        input_tokens=5,
        cached_input_tokens=0,
        output_tokens=2,
        reasoning_tokens=1,
        total_tokens=7,
    )
    with pytest.raises(PricingManifestError, match="pricing_identity"):
        ledger.settle_known_usage(
            scope_id="turn-1",
            attempt_id=reservation.attempt_id,
            usage=usage,
            response_id="resp_wrong",
            response_model="gpt-5.4-nano-2026-03-17",
            response_service_tier="default",
        )
    summary = ledger.scope_usage_summary("turn-1")
    assert summary.costs.unknown_nano_usd == reservation.quote.reserved_nano_usd
    assert summary.total_tokens == 7
    assert summary.unknown_cost_attempts == 1


def test_deadline_recheck_and_recovery_retire_active_slots(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    now = [datetime(2026, 9, 9, 10, 0, tzinfo=UTC)]
    ledger = SQLProviderBudgetLedger(
        factory,
        PricingManifest.load(default_pricing_manifest_path()),
        clock=lambda: now[0],
    )
    try:
        ledger.create_account(account_id="shared")
        ledger.create_scope(
            scope_id="undispatched",
            account_id="shared",
            purpose="chat",
            deadline_at=now[0] + timedelta(seconds=5),
        )
        first = _reserve(ledger, scope_id="undispatched")
        now[0] += timedelta(seconds=6)
        with pytest.raises(BudgetDeadlineError):
            ledger.mark_dispatched(scope_id="undispatched", attempt_id=first.attempt_id)
        assert ledger.scope_summary("undispatched").encumbered_nano_usd == 0
        assert not ledger.mark_dispatched(
            scope_id="undispatched", attempt_id=first.attempt_id
        )

        ledger.create_scope(
            scope_id="dispatched",
            account_id="shared",
            purpose="chat",
            deadline_at=now[0] + timedelta(seconds=10),
        )
        second = _reserve(ledger, scope_id="dispatched")
        ledger.mark_dispatched(scope_id="dispatched", attempt_id=second.attempt_id)
        now[0] += timedelta(seconds=2)
        assert ledger.recover_expired_attempts(scope_id="dispatched") == 0
        now[0] += timedelta(seconds=17)
        assert ledger.recover_expired_attempts(scope_id="dispatched") == 1
        assert (
            ledger.scope_summary("dispatched").unknown_nano_usd
            == second.quote.reserved_nano_usd
        )
        assert not ledger.mark_dispatched(
            scope_id="dispatched", attempt_id=second.attempt_id
        )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_generation_logical_call_and_combined_attempt_caps(
    ledger_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session]],
) -> None:
    ledger, factory = ledger_store
    _create_scope(ledger, scope_id="generation-cap")
    usage = ProviderUsage(
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=1,
        reasoning_tokens=0,
        total_tokens=2,
    )
    for index in range(10):
        reservation = _reserve(
            ledger,
            scope_id="generation-cap",
            call_id=f"mcall_{index:032x}",
            input_bound=1,
            output_bound=1,
        )
        ledger.mark_dispatched(
            scope_id="generation-cap", attempt_id=reservation.attempt_id
        )
        ledger.settle_known_usage(
            scope_id="generation-cap",
            attempt_id=reservation.attempt_id,
            usage=usage,
            response_id=f"resp_{index}",
            response_model="gpt-5.4-mini-2026-03-17",
            response_service_tier="default",
        )
        ledger.record_attempt_result(
            scope_id="generation-cap",
            attempt_id=reservation.attempt_id,
            result_status="success",
        )
    with pytest.raises(BudgetAttemptLimitError, match="generation_call_limit"):
        _reserve(
            ledger,
            scope_id="generation-cap",
            call_id="mcall_ffffffffffffffffffffffffffffffff",
            input_bound=1,
            output_bound=1,
        )
    with factory() as session:
        scope = session.get(ProviderBudgetScope, "generation-cap")
        assert scope is not None
        assert scope.generation_calls == 10

    _create_scope(ledger, scope_id="attempt-cap", purpose="benchmark")
    for logical_index in range(8):
        call_id = f"mcall_{logical_index + 100:032x}"
        for attempt_number in (1, 2):
            reservation = ledger.reserve_attempt(
                scope_id="attempt-cap",
                call_id=call_id,
                attempt_number=attempt_number,
                operation="generation" if logical_index < 4 else "embedding",
                purpose="benchmark",
                model=(
                    "gpt-5.4-mini" if logical_index < 4 else "text-embedding-3-small"
                ),
                request_fingerprint_sha256=f"{logical_index + 1:064x}",
                input_token_bound=1,
                output_token_bound=1 if logical_index < 4 else 0,
            )
            ledger.mark_dispatched(
                scope_id="attempt-cap", attempt_id=reservation.attempt_id
            )
            ledger.settle_unknown_usage(
                scope_id="attempt-cap",
                attempt_id=reservation.attempt_id,
                result_status="error",
                error_code="synthetic_retry",
            )
    with pytest.raises(BudgetAttemptLimitError, match="provider_attempt_limit"):
        ledger.reserve_attempt(
            scope_id="attempt-cap",
            call_id="ecall_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            attempt_number=1,
            operation="embedding",
            purpose="benchmark",
            model="text-embedding-3-small",
            request_fingerprint_sha256="e" * 64,
            input_token_bound=1,
            output_token_bound=0,
        )
    with pytest.raises(BudgetAttemptLimitError, match="retry_limit"):
        ledger.reserve_attempt(
            scope_id="attempt-cap",
            call_id="ecall_dddddddddddddddddddddddddddddddd",
            attempt_number=3,
            operation="embedding",
            purpose="benchmark",
            model="text-embedding-3-small",
            request_fingerprint_sha256="d" * 64,
            input_token_bound=1,
            output_token_bound=0,
        )


def test_postgres_concurrent_reservations_enforce_cost_and_global_concurrency() -> None:
    with disposable_postgres_database("thanh_v2_p2_budget_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_size=6, max_overflow=0)
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        manifest = PricingManifest.load(default_pricing_manifest_path())
        ledger = SQLProviderBudgetLedger(factory, manifest)
        reservation_cost = manifest.quote(
            model="gpt-5.4-mini",
            operation="generation",
            input_token_bound=100,
            output_token_bound=10,
        ).reserved_nano_usd
        try:
            ledger.create_account(
                account_id="cost-ceiling",
                hard_limit_nano_usd=reservation_cost,
                warning_threshold_nano_usd=reservation_cost,
            )
            for index in range(2):
                ledger.create_scope(
                    scope_id=f"cost-scope-{index}",
                    account_id="cost-ceiling",
                    purpose="chat",
                    hard_limit_nano_usd=reservation_cost,
                )
            barrier = Barrier(2)

            def reserve_cost(index: int) -> str:
                barrier.wait()
                try:
                    _reserve(
                        ledger,
                        scope_id=f"cost-scope-{index}",
                        call_id=f"mcall_{index:032x}",
                    )
                except BudgetLimitExceededError:
                    return "limited"
                return "reserved"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(reserve_cost, range(2)))
            assert sorted(outcomes) == ["limited", "reserved"]
            assert ledger.account_summary("cost-ceiling").reserved_nano_usd == (
                reservation_cost
            )

            ledger.create_account(account_id="concurrency")
            for index in range(3):
                ledger.create_scope(
                    scope_id=f"concurrency-scope-{index}",
                    account_id="concurrency",
                    purpose="chat",
                )
            concurrency_barrier = Barrier(3)

            def reserve_concurrent(index: int) -> str:
                concurrency_barrier.wait()
                try:
                    _reserve(
                        ledger,
                        scope_id=f"concurrency-scope-{index}",
                        call_id=f"mcall_{index + 10:032x}",
                    )
                except BudgetConcurrencyError:
                    return "limited"
                return "reserved"

            with ThreadPoolExecutor(max_workers=3) as executor:
                concurrency_outcomes = list(executor.map(reserve_concurrent, range(3)))
            assert sorted(concurrency_outcomes) == [
                "limited",
                "reserved",
                "reserved",
            ]
            with factory() as session:
                account = session.scalar(
                    select(ProviderBudgetAccount).where(
                        ProviderBudgetAccount.account_id == "concurrency"
                    )
                )
                attempts = list(
                    session.scalars(
                        select(ProviderAttempt).where(
                            ProviderAttempt.scope_id.like("concurrency-scope-%")
                        )
                    )
                )
            assert account is not None and account.active_attempts == 2
            assert len(attempts) == 2
        finally:
            engine.dispose()
