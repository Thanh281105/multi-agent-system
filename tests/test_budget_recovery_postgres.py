"""Real PostgreSQL races for durable provider-attempt recovery."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.migrate import upgrade_database
from app.models.budget import (
    ProviderAttempt,
    ProviderBudgetAccount,
    ProviderBudgetScope,
)
from app.shared.budget import (
    BudgetAccountingError,
    BudgetConflictError,
    PricingManifest,
    ProviderUsage,
    SQLProviderBudgetLedger,
    default_pricing_manifest_path,
)
from tests.v2_postgres_support import disposable_postgres_database

_RESPONSE_MODEL = "gpt-5.4-mini-2026-03-17"
_SERVICE_TIER = "default"
_USAGE = ProviderUsage(
    input_tokens=100,
    cached_input_tokens=20,
    output_tokens=10,
    reasoning_tokens=3,
    total_tokens=110,
)
_DIFFERENT_USAGE = ProviderUsage(
    input_tokens=99,
    cached_input_tokens=19,
    output_tokens=10,
    reasoning_tokens=2,
    total_tokens=109,
)


@dataclass
class MutableClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


@dataclass(frozen=True)
class AttemptGraphState:
    account_known: int
    account_reserved: int
    account_unknown: int
    account_active: int
    scope_known: int
    scope_reserved: int
    scope_unknown: int
    scope_active: int
    usage_status: str
    result_status: str
    error_code: str | None
    actual_cost: int | None
    input_tokens: int | None
    reasoning_tokens: int | None
    transport_started: bool


@pytest.fixture(scope="module")
def postgres_store() -> Iterator[tuple[sessionmaker[Session], PricingManifest]]:
    with disposable_postgres_database("thanh_v2_p2_recovery_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=0,
        )
        factory = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
        manifest = PricingManifest.load(default_pricing_manifest_path())
        try:
            yield factory, manifest
        finally:
            engine.dispose()


def _ledger(
    store: tuple[sessionmaker[Session], PricingManifest],
) -> tuple[SQLProviderBudgetLedger, MutableClock]:
    factory, manifest = store
    clock = MutableClock(datetime(2026, 9, 9, 12, 0, tzinfo=UTC))
    return SQLProviderBudgetLedger(factory, manifest, clock=clock), clock


def _create_attempt(
    ledger: SQLProviderBudgetLedger,
    case: str,
    *,
    dispatch: bool = True,
) -> tuple[str, str, str, int]:
    account_id = f"account-{case}"
    scope_id = f"scope-{case}"
    ledger.create_account(account_id=account_id)
    ledger.create_scope(
        scope_id=scope_id,
        account_id=account_id,
        purpose="chat",
    )
    reservation = ledger.reserve_attempt(
        scope_id=scope_id,
        call_id=f"call-{case}",
        attempt_number=1,
        operation="generation",
        purpose="chat",
        model="gpt-5.4-mini",
        request_fingerprint_sha256="a" * 64,
        input_token_bound=100,
        output_token_bound=10,
        attempt_timeout_seconds=1,
    )
    if dispatch:
        assert ledger.mark_dispatched(
            scope_id=scope_id,
            attempt_id=reservation.attempt_id,
        )
    return (
        account_id,
        scope_id,
        reservation.attempt_id,
        reservation.quote.reserved_nano_usd,
    )


def _settle_known(
    ledger: SQLProviderBudgetLedger,
    *,
    scope_id: str,
    attempt_id: str,
    usage: ProviderUsage = _USAGE,
    response_id: str = "resp-recovery",
) -> None:
    ledger.settle_known_usage(
        scope_id=scope_id,
        attempt_id=attempt_id,
        usage=usage,
        response_id=response_id,
        response_model=_RESPONSE_MODEL,
        response_service_tier=_SERVICE_TIER,
    )


def _state(
    factory: sessionmaker[Session],
    *,
    account_id: str,
    scope_id: str,
    attempt_id: str,
) -> AttemptGraphState:
    with factory() as session:
        account = session.get(ProviderBudgetAccount, account_id)
        scope = session.get(ProviderBudgetScope, scope_id)
        attempt = session.get(ProviderAttempt, attempt_id)
        assert account is not None
        assert scope is not None
        assert attempt is not None
        return AttemptGraphState(
            account_known=account.known_cost_nano_usd,
            account_reserved=account.reserved_cost_nano_usd,
            account_unknown=account.unknown_cost_nano_usd,
            account_active=account.active_attempts,
            scope_known=scope.known_cost_nano_usd,
            scope_reserved=scope.reserved_cost_nano_usd,
            scope_unknown=scope.unknown_cost_nano_usd,
            scope_active=scope.active_attempts,
            usage_status=attempt.usage_status,
            result_status=attempt.result_status,
            error_code=attempt.error_code,
            actual_cost=attempt.actual_cost_nano_usd,
            input_tokens=attempt.input_tokens,
            reasoning_tokens=attempt.reasoning_tokens,
            transport_started=attempt.transport_started_at is not None,
        )


def _assert_exact_known(
    state: AttemptGraphState,
    *,
    expected_cost: int,
) -> None:
    assert (state.account_known, state.scope_known) == (
        expected_cost,
        expected_cost,
    )
    assert (state.account_reserved, state.scope_reserved) == (0, 0)
    assert (state.account_unknown, state.scope_unknown) == (0, 0)
    assert (state.account_active, state.scope_active) == (0, 0)
    assert state.usage_status == "known"
    assert state.actual_cost == expected_cost
    assert state.input_tokens == _USAGE.input_tokens
    assert state.reasoning_tokens == _USAGE.reasoning_tokens
    assert state.transport_started


def _lock_graph(
    session: Session,
    *,
    account_id: str,
    scope_id: str,
    attempt_id: str,
) -> None:
    session.execute(text("SET LOCAL lock_timeout = '1s'"))
    session.execute(
        text(
            "SELECT account_id FROM v2_budget_accounts "
            "WHERE account_id = :account_id FOR UPDATE"
        ),
        {"account_id": account_id},
    ).one()
    session.execute(
        text(
            "SELECT scope_id FROM v2_budget_scopes "
            "WHERE scope_id = :scope_id FOR UPDATE"
        ),
        {"scope_id": scope_id},
    ).one()
    session.execute(
        text(
            "SELECT attempt_id FROM v2_provider_attempts "
            "WHERE attempt_id = :attempt_id FOR UPDATE"
        ),
        {"attempt_id": attempt_id},
    ).one()
    session.commit()


def test_recovery_then_known_usage_reconciles_once_and_preserves_timeout(
    postgres_store: tuple[sessionmaker[Session], PricingManifest],
) -> None:
    factory, manifest = postgres_store
    ledger, clock = _ledger(postgres_store)
    account_id, scope_id, attempt_id, reserved_cost = _create_attempt(
        ledger, "recover-first"
    )
    expected_cost = manifest.known_cost(
        resolved_model=_RESPONSE_MODEL,
        usage=_USAGE,
    )
    clock.advance(seconds=2)

    assert ledger.recover_expired_attempts(scope_id=scope_id) == 1
    recovered = _state(
        factory,
        account_id=account_id,
        scope_id=scope_id,
        attempt_id=attempt_id,
    )
    assert (recovered.account_reserved, recovered.scope_reserved) == (0, 0)
    assert (recovered.account_unknown, recovered.scope_unknown) == (
        reserved_cost,
        reserved_cost,
    )
    assert (recovered.account_active, recovered.scope_active) == (0, 0)
    assert (recovered.usage_status, recovered.result_status) == (
        "unknown",
        "timeout",
    )
    assert recovered.error_code == "provider_attempt_recovered_after_deadline"

    _settle_known(ledger, scope_id=scope_id, attempt_id=attempt_id)
    reconciled = _state(
        factory,
        account_id=account_id,
        scope_id=scope_id,
        attempt_id=attempt_id,
    )
    _assert_exact_known(reconciled, expected_cost=expected_cost)
    assert reconciled.result_status == "timeout"
    assert reconciled.error_code == "provider_attempt_recovered_after_deadline"

    _settle_known(ledger, scope_id=scope_id, attempt_id=attempt_id)
    assert (
        _state(
            factory,
            account_id=account_id,
            scope_id=scope_id,
            attempt_id=attempt_id,
        )
        == reconciled
    )
    with pytest.raises(BudgetConflictError, match="known_settlement_conflict"):
        _settle_known(
            ledger,
            scope_id=scope_id,
            attempt_id=attempt_id,
            usage=_DIFFERENT_USAGE,
        )
    assert (
        _state(
            factory,
            account_id=account_id,
            scope_id=scope_id,
            attempt_id=attempt_id,
        )
        == reconciled
    )


def test_late_known_usage_preserves_cancelled_terminal(
    postgres_store: tuple[sessionmaker[Session], PricingManifest],
) -> None:
    factory, manifest = postgres_store
    ledger, _ = _ledger(postgres_store)
    account_id, scope_id, attempt_id, _ = _create_attempt(
        ledger, "cancelled-late-usage"
    )
    ledger.settle_unknown_usage(
        scope_id=scope_id,
        attempt_id=attempt_id,
        result_status="cancelled",
        error_code="provider_attempt_cancelled",
    )

    _settle_known(ledger, scope_id=scope_id, attempt_id=attempt_id)
    state = _state(
        factory,
        account_id=account_id,
        scope_id=scope_id,
        attempt_id=attempt_id,
    )
    _assert_exact_known(
        state,
        expected_cost=manifest.known_cost(
            resolved_model=_RESPONSE_MODEL,
            usage=_USAGE,
        ),
    )
    assert state.result_status == "cancelled"
    assert state.error_code == "provider_attempt_cancelled"


def test_known_usage_then_recovery_is_noop(
    postgres_store: tuple[sessionmaker[Session], PricingManifest],
) -> None:
    factory, manifest = postgres_store
    ledger, clock = _ledger(postgres_store)
    account_id, scope_id, attempt_id, _ = _create_attempt(ledger, "settlement-first")
    _settle_known(ledger, scope_id=scope_id, attempt_id=attempt_id)
    ledger.record_attempt_result(
        scope_id=scope_id,
        attempt_id=attempt_id,
        result_status="success",
    )
    clock.advance(seconds=2)

    assert ledger.recover_expired_attempts(scope_id=scope_id) == 0
    state = _state(
        factory,
        account_id=account_id,
        scope_id=scope_id,
        attempt_id=attempt_id,
    )
    _assert_exact_known(
        state,
        expected_cost=manifest.known_cost(
            resolved_model=_RESPONSE_MODEL,
            usage=_USAGE,
        ),
    )
    assert state.result_status == "success"
    assert state.error_code is None


def test_recovery_and_known_usage_race_has_one_bucket_transition(
    postgres_store: tuple[sessionmaker[Session], PricingManifest],
) -> None:
    factory, manifest = postgres_store
    ledger, clock = _ledger(postgres_store)
    account_id, scope_id, attempt_id, _ = _create_attempt(ledger, "recover-race")
    clock.advance(seconds=2)
    barrier = Barrier(2)

    def recover() -> int:
        barrier.wait(timeout=10)
        return ledger.recover_expired_attempts(scope_id=scope_id)

    def settle() -> str:
        barrier.wait(timeout=10)
        _settle_known(ledger, scope_id=scope_id, attempt_id=attempt_id)
        return "settled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery_future = executor.submit(recover)
        settlement_future = executor.submit(settle)
        recovered = recovery_future.result(timeout=15)
        assert settlement_future.result(timeout=15) == "settled"

    assert recovered in (0, 1)
    state = _state(
        factory,
        account_id=account_id,
        scope_id=scope_id,
        attempt_id=attempt_id,
    )
    _assert_exact_known(
        state,
        expected_cost=manifest.known_cost(
            resolved_model=_RESPONSE_MODEL,
            usage=_USAGE,
        ),
    )
    if recovered == 1:
        assert state.result_status == "timeout"
        assert state.error_code == "provider_attempt_recovered_after_deadline"
    else:
        assert state.result_status == "pending"
        assert state.error_code is None


def test_abandon_and_dispatch_orderings_and_race(
    postgres_store: tuple[sessionmaker[Session], PricingManifest],
) -> None:
    factory, _ = postgres_store
    ledger, _ = _ledger(postgres_store)

    abandoned_ids = _create_attempt(ledger, "abandon-first", dispatch=False)
    abandoned_account, abandoned_scope, abandoned_attempt, _ = abandoned_ids
    ledger.abandon_undispatched(
        scope_id=abandoned_scope,
        attempt_id=abandoned_attempt,
        error_code="provider_dispatch_cancelled",
    )
    assert not ledger.mark_dispatched(
        scope_id=abandoned_scope,
        attempt_id=abandoned_attempt,
    )
    abandoned = _state(
        factory,
        account_id=abandoned_account,
        scope_id=abandoned_scope,
        attempt_id=abandoned_attempt,
    )
    assert (abandoned.usage_status, abandoned.result_status) == (
        "known",
        "cancelled",
    )
    assert abandoned.error_code == "provider_dispatch_cancelled"
    assert abandoned.actual_cost == 0
    assert not abandoned.transport_started
    assert (abandoned.account_active, abandoned.scope_active) == (0, 0)
    assert (
        abandoned.account_known + abandoned.account_reserved + abandoned.account_unknown
    ) == 0

    dispatched_ids = _create_attempt(ledger, "dispatch-first")
    dispatched_account, dispatched_scope, dispatched_attempt, dispatched_cost = (
        dispatched_ids
    )
    with pytest.raises(
        BudgetAccountingError,
        match="dispatched_attempt_cannot_be_abandoned",
    ):
        ledger.abandon_undispatched(
            scope_id=dispatched_scope,
            attempt_id=dispatched_attempt,
            error_code="provider_dispatch_cancelled",
        )
    dispatched = _state(
        factory,
        account_id=dispatched_account,
        scope_id=dispatched_scope,
        attempt_id=dispatched_attempt,
    )
    assert dispatched.usage_status == "reserved"
    assert dispatched.transport_started
    assert (dispatched.account_reserved, dispatched.scope_reserved) == (
        dispatched_cost,
        dispatched_cost,
    )
    assert (dispatched.account_active, dispatched.scope_active) == (1, 1)
    ledger.settle_unknown_usage(
        scope_id=dispatched_scope,
        attempt_id=dispatched_attempt,
        result_status="cancelled",
        error_code="provider_attempt_cancelled",
    )
    dispatched = _state(
        factory,
        account_id=dispatched_account,
        scope_id=dispatched_scope,
        attempt_id=dispatched_attempt,
    )
    assert (dispatched.account_unknown, dispatched.scope_unknown) == (
        dispatched_cost,
        dispatched_cost,
    )
    assert (dispatched.account_active, dispatched.scope_active) == (0, 0)

    race_account, race_scope, race_attempt, race_cost = _create_attempt(
        ledger, "abandon-dispatch-race", dispatch=False
    )
    barrier = Barrier(2)

    def abandon() -> str:
        barrier.wait(timeout=10)
        try:
            ledger.abandon_undispatched(
                scope_id=race_scope,
                attempt_id=race_attempt,
                error_code="provider_dispatch_cancelled",
            )
        except BudgetAccountingError as exc:
            assert str(exc) == "dispatched_attempt_cannot_be_abandoned"
            return "dispatch-won"
        return "abandon-won"

    def dispatch() -> bool:
        barrier.wait(timeout=10)
        return ledger.mark_dispatched(
            scope_id=race_scope,
            attempt_id=race_attempt,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        abandon_future = executor.submit(abandon)
        dispatch_future = executor.submit(dispatch)
        abandon_outcome = abandon_future.result(timeout=15)
        dispatched_by_race = dispatch_future.result(timeout=15)

    assert (abandon_outcome, dispatched_by_race) in (
        ("abandon-won", False),
        ("dispatch-won", True),
    )
    assert not ledger.mark_dispatched(scope_id=race_scope, attempt_id=race_attempt)
    race_state = _state(
        factory,
        account_id=race_account,
        scope_id=race_scope,
        attempt_id=race_attempt,
    )
    if dispatched_by_race:
        assert race_state.usage_status == "reserved"
        assert race_state.transport_started
        assert (race_state.account_active, race_state.scope_active) == (1, 1)
        ledger.settle_unknown_usage(
            scope_id=race_scope,
            attempt_id=race_attempt,
            result_status="cancelled",
            error_code="provider_attempt_cancelled",
        )
        race_state = _state(
            factory,
            account_id=race_account,
            scope_id=race_scope,
            attempt_id=race_attempt,
        )
        assert (race_state.account_unknown, race_state.scope_unknown) == (
            race_cost,
            race_cost,
        )
    else:
        assert (race_state.usage_status, race_state.result_status) == (
            "known",
            "cancelled",
        )
        assert not race_state.transport_started
        assert (race_state.account_unknown, race_state.scope_unknown) == (0, 0)
    assert (race_state.account_active, race_state.scope_active) == (0, 0)


def test_duplicate_settlements_and_conflicts_release_graph_locks(
    postgres_store: tuple[sessionmaker[Session], PricingManifest],
) -> None:
    factory, manifest = postgres_store
    ledger, _ = _ledger(postgres_store)

    known_account, known_scope, known_attempt, _ = _create_attempt(
        ledger, "duplicate-known"
    )
    known_barrier = Barrier(2)

    def settle_known_duplicate() -> str:
        known_barrier.wait(timeout=10)
        _settle_known(
            ledger,
            scope_id=known_scope,
            attempt_id=known_attempt,
            response_id="resp-known-duplicate",
        )
        return "settled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: settle_known_duplicate(), range(2))) == [
            "settled",
            "settled",
        ]
    known = _state(
        factory,
        account_id=known_account,
        scope_id=known_scope,
        attempt_id=known_attempt,
    )
    _assert_exact_known(
        known,
        expected_cost=manifest.known_cost(
            resolved_model=_RESPONSE_MODEL,
            usage=_USAGE,
        ),
    )

    probe_session = factory()
    try:
        _settle_known(
            ledger,
            scope_id=known_scope,
            attempt_id=known_attempt,
            response_id="resp-known-duplicate",
        )
        _lock_graph(
            probe_session,
            account_id=known_account,
            scope_id=known_scope,
            attempt_id=known_attempt,
        )
        with pytest.raises(BudgetConflictError, match="known_settlement_conflict"):
            _settle_known(
                ledger,
                scope_id=known_scope,
                attempt_id=known_attempt,
                response_id="resp-known-conflict",
            )
        _lock_graph(
            probe_session,
            account_id=known_account,
            scope_id=known_scope,
            attempt_id=known_attempt,
        )
    finally:
        probe_session.close()

    unknown_account, unknown_scope, unknown_attempt, unknown_cost = _create_attempt(
        ledger, "duplicate-unknown"
    )
    unknown_barrier = Barrier(2)

    def settle_unknown_duplicate() -> str:
        unknown_barrier.wait(timeout=10)
        ledger.settle_unknown_usage(
            scope_id=unknown_scope,
            attempt_id=unknown_attempt,
            response_id="resp-unknown-duplicate",
            response_model=_RESPONSE_MODEL,
            response_service_tier=_SERVICE_TIER,
            result_status="timeout",
            error_code="model_timeout",
        )
        return "settled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: settle_unknown_duplicate(), range(2))) == [
            "settled",
            "settled",
        ]
    unknown = _state(
        factory,
        account_id=unknown_account,
        scope_id=unknown_scope,
        attempt_id=unknown_attempt,
    )
    assert (unknown.account_reserved, unknown.scope_reserved) == (0, 0)
    assert (unknown.account_unknown, unknown.scope_unknown) == (
        unknown_cost,
        unknown_cost,
    )
    assert (unknown.account_active, unknown.scope_active) == (0, 0)
    assert (unknown.usage_status, unknown.result_status) == ("unknown", "timeout")

    probe_session = factory()
    try:
        ledger.settle_unknown_usage(
            scope_id=unknown_scope,
            attempt_id=unknown_attempt,
            response_id="resp-unknown-duplicate",
            response_model=_RESPONSE_MODEL,
            response_service_tier=_SERVICE_TIER,
            result_status="timeout",
            error_code="model_timeout",
        )
        _lock_graph(
            probe_session,
            account_id=unknown_account,
            scope_id=unknown_scope,
            attempt_id=unknown_attempt,
        )
        with pytest.raises(BudgetConflictError, match="unknown_settlement_conflict"):
            ledger.settle_unknown_usage(
                scope_id=unknown_scope,
                attempt_id=unknown_attempt,
                response_id="resp-unknown-conflict",
                response_model=_RESPONSE_MODEL,
                response_service_tier=_SERVICE_TIER,
                result_status="timeout",
                error_code="model_timeout",
            )
        _lock_graph(
            probe_session,
            account_id=unknown_account,
            scope_id=unknown_scope,
            attempt_id=unknown_attempt,
        )
    finally:
        probe_session.close()
