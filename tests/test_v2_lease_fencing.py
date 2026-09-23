"""Real PostgreSQL coverage for atomic v2 turn lease fences."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import sleep

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext, TaskStatus
from app.db.migrate import upgrade_database
from app.db.v2_repository import (
    StepResultConflictError,
    TurnLeaseConflictError,
    TurnStateConflictError,
    V2Repository,
)
from app.models.v2 import V2StepResult, V2Turn
from app.v2.authorization import AuthorizationDeniedError, ResourceNotFoundError
from app.v2.contracts import (
    ConversationMode,
    DialogueOutcome,
    SafeExecutionError,
    TurnStatus,
)
from tests.v2_postgres_support import disposable_postgres_database


@pytest.fixture(scope="module")
def postgres_sessions() -> Iterator[sessionmaker[Session]]:
    with disposable_postgres_database("thanh_v2_p2_lease_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
        try:
            yield sessions
        finally:
            engine.dispose()


def test_active_step_and_terminal_writes_recheck_owner_and_scope(
    postgres_sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(postgres_sessions, "active_guard")
    worker = "worker_active_guard"
    _claim(postgres_sessions, authorization, turn_id, worker)
    foreign = _auth(authorization.tenant_id, "principal_foreign", "ecommerce.read")
    revoked = _auth(authorization.tenant_id, authorization.principal_id)

    for credential, exception in (
        (foreign, ResourceNotFoundError),
        (revoked, AuthorizationDeniedError),
    ):
        with postgres_sessions() as session:
            with pytest.raises(exception):
                _persist_step(
                    V2Repository(session), credential, turn_id, worker, "forbidden"
                )

    with postgres_sessions() as session:
        repository = V2Repository(session)
        with pytest.raises(TurnLeaseConflictError, match="current"):
            _persist_step(repository, authorization, turn_id, "worker_wrong", "wrong")
        assert (
            repository.get_step_result(
                authorization,
                turn_id=turn_id,
                operation_key=_operation_key(turn_id),
            )
            is None
        )
        stored = _persist_step(repository, authorization, turn_id, worker, "accepted")
        assert stored.status == TaskStatus.SUCCESS.value

    terminal_authorization, terminal_id = _pending(postgres_sessions, "active_terminal")
    terminal_worker = "worker_active_terminal"
    _claim(
        postgres_sessions,
        terminal_authorization,
        terminal_id,
        terminal_worker,
    )
    terminal_revoked = _auth(
        terminal_authorization.tenant_id, terminal_authorization.principal_id
    )
    with postgres_sessions() as session:
        with pytest.raises(AuthorizationDeniedError):
            _complete_answered(
                V2Repository(session),
                terminal_revoked,
                terminal_id,
                lease_owner=terminal_worker,
                expected_status=TurnStatus.RUNNING,
            )
    with postgres_sessions() as session:
        repository = V2Repository(session)
        with pytest.raises(TurnLeaseConflictError, match="current"):
            _complete_answered(
                repository,
                terminal_authorization,
                terminal_id,
                lease_owner="worker_wrong",
                expected_status=TurnStatus.RUNNING,
            )
        completed = _complete_answered(
            repository,
            terminal_authorization,
            terminal_id,
            lease_owner=terminal_worker,
            expected_status=TurnStatus.RUNNING,
        )
        assert completed.execution_state == TurnStatus.COMPLETED.value


def test_prior_live_observation_cannot_authorize_a_late_write(
    postgres_sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(postgres_sessions, "late_write")
    worker = "worker_late_write"
    _claim(postgres_sessions, authorization, turn_id, worker)
    with postgres_sessions() as session:
        observed = V2Repository(session).get_turn(authorization, turn_id)
        assert observed.lease_expires_at is not None
        assert _aware(observed.lease_expires_at) > datetime.now(UTC)
    with postgres_sessions() as session:
        session.execute(
            update(V2Turn)
            .where(V2Turn.id == turn_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()

    with postgres_sessions() as session:
        repository = V2Repository(session)
        with pytest.raises(TurnLeaseConflictError, match="current"):
            _persist_step(repository, authorization, turn_id, worker, "late")
        with pytest.raises(TurnLeaseConflictError, match="current"):
            _complete_answered(
                repository,
                authorization,
                turn_id,
                lease_owner=worker,
                expected_status=TurnStatus.RUNNING,
            )
        restored = repository.get_turn(authorization, turn_id)
        assert restored.execution_state == TurnStatus.RUNNING.value
        assert restored.result is None
        assert (
            repository.get_step_result(
                authorization,
                turn_id=turn_id,
                operation_key=_operation_key(turn_id),
            )
            is None
        )


def test_turn_row_lock_orders_terminal_transition_before_late_step_write(
    postgres_sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(postgres_sessions, "lock_race")
    worker = "worker_lock_race"
    _claim(postgres_sessions, authorization, turn_id, worker)
    terminal_locked = Event()
    release_terminal = Event()
    writer_started = Event()

    def terminate() -> str:
        with postgres_sessions() as session:
            session.scalar(select(V2Turn).where(V2Turn.id == turn_id).with_for_update())
            terminal_locked.set()
            if not release_terminal.wait(timeout=10):
                raise AssertionError("terminal transition was not released")
            V2Repository(session).complete_turn(
                authorization,
                turn_id,
                status=TurnStatus.CANCELLED,
                lease_owner=worker,
                expected_status=TurnStatus.RUNNING,
            )
            return "cancelled"

    def persist_late_step() -> str:
        if not terminal_locked.wait(timeout=10):
            raise AssertionError("terminal row lock was not acquired")
        writer_started.set()
        with postgres_sessions() as session:
            try:
                _persist_step(
                    V2Repository(session), authorization, turn_id, worker, "late"
                )
            except TurnLeaseConflictError:
                return "conflict"
            return "persisted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        terminal_future = executor.submit(terminate)
        assert terminal_locked.wait(timeout=10)
        writer_future = executor.submit(persist_late_step)
        assert writer_started.wait(timeout=10)
        try:
            sleep(0.15)
            assert not writer_future.done()
        finally:
            release_terminal.set()
        assert terminal_future.result(timeout=10) == "cancelled"
        assert writer_future.result(timeout=10) == "conflict"

    with postgres_sessions() as session:
        repository = V2Repository(session)
        assert (
            repository.get_turn(authorization, turn_id).execution_state
            == TurnStatus.CANCELLED.value
        )
        assert (
            repository.get_step_result(
                authorization,
                turn_id=turn_id,
                operation_key=_operation_key(turn_id),
            )
            is None
        )


def test_exact_replays_remain_immutable_after_completion(
    postgres_sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(postgres_sessions, "immutable_replay")
    worker = "worker_immutable_replay"
    _claim(postgres_sessions, authorization, turn_id, worker)
    with postgres_sessions() as session:
        repository = V2Repository(session)
        step = _persist_step(repository, authorization, turn_id, worker, "stable")
        completed = _complete_answered(
            repository,
            authorization,
            turn_id,
            lease_owner=worker,
            expected_status=TurnStatus.RUNNING,
            answer="stable answer",
        )

        replayed_step = _persist_step(
            repository, authorization, turn_id, worker, "stable"
        )
        assert replayed_step.id == step.id
        with pytest.raises(StepResultConflictError):
            _persist_step(repository, authorization, turn_id, worker, "changed")

        replayed_turn = _complete_answered(
            repository,
            authorization,
            turn_id,
            lease_owner=worker,
            expected_status=TurnStatus.RUNNING,
            answer="stable answer",
        )
        assert replayed_turn.id == completed.id
        with pytest.raises(TurnStateConflictError):
            _complete_answered(
                repository,
                authorization,
                turn_id,
                lease_owner=worker,
                expected_status=TurnStatus.RUNNING,
                answer="changed answer",
            )


def test_expired_recovery_rejects_live_turn_and_accepts_expired_or_missing_lease(
    postgres_sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(postgres_sessions, "expired_recovery")
    worker = "worker_expired_recovery"
    _claim(postgres_sessions, authorization, turn_id, worker)
    recovery_error = SafeExecutionError(
        code="turn_lease_expired",
        message="The active worker lease expired.",
        retryable=True,
    )
    with postgres_sessions() as session:
        repository = V2Repository(session)
        with pytest.raises(TurnLeaseConflictError, match="expired or missing"):
            repository.complete_turn(
                authorization,
                turn_id,
                status=TurnStatus.INTERRUPTED,
                safe_error=recovery_error,
                expected_status=TurnStatus.RUNNING,
                require_expired_lease=True,
            )
        with pytest.raises(ValueError, match="cannot be combined"):
            repository.complete_turn(
                authorization,
                turn_id,
                status=TurnStatus.INTERRUPTED,
                safe_error=recovery_error,
                lease_owner=worker,
                expected_status=TurnStatus.RUNNING,
                require_expired_lease=True,
            )
    _set_lease_expiry(
        postgres_sessions,
        turn_id,
        datetime.now(UTC) - timedelta(seconds=1),
    )
    with postgres_sessions() as session:
        recovered = V2Repository(session).complete_turn(
            authorization,
            turn_id,
            status=TurnStatus.INTERRUPTED,
            safe_error=recovery_error,
            expected_status=TurnStatus.RUNNING,
            require_expired_lease=True,
        )
        assert recovered.execution_state == TurnStatus.INTERRUPTED.value

    missing_authorization, missing_id = _pending(
        postgres_sessions, "missing_lease_recovery"
    )
    _claim(
        postgres_sessions,
        missing_authorization,
        missing_id,
        "worker_missing_lease",
    )
    _set_lease_expiry(postgres_sessions, missing_id, None)
    with postgres_sessions() as session:
        recovered = V2Repository(session).complete_turn(
            missing_authorization,
            missing_id,
            status=TurnStatus.INTERRUPTED,
            safe_error=recovery_error,
            expected_status=TurnStatus.RUNNING,
            require_expired_lease=True,
        )
        assert recovered.execution_state == TurnStatus.INTERRUPTED.value


def test_pending_only_failure_cannot_terminate_a_running_claimant(
    postgres_sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(postgres_sessions, "pending_fence")
    _claim(postgres_sessions, authorization, turn_id, "worker_pending_fence")
    pin_error = SafeExecutionError(
        code="turn_runtime_pin_conflict",
        message="The runtime pin changed before the turn was claimed.",
        retryable=False,
    )
    with postgres_sessions() as session:
        repository = V2Repository(session)
        with pytest.raises(TurnLeaseConflictError, match="expected state"):
            repository.complete_turn(
                authorization,
                turn_id,
                status=TurnStatus.INTERRUPTED,
                safe_error=pin_error,
                expected_status=TurnStatus.PENDING,
            )
        assert (
            repository.get_turn(authorization, turn_id).execution_state
            == TurnStatus.RUNNING.value
        )

    pending_authorization, pending_id = _pending(
        postgres_sessions, "pending_fence_success"
    )
    with postgres_sessions() as session:
        interrupted = V2Repository(session).complete_turn(
            pending_authorization,
            pending_id,
            status=TurnStatus.INTERRUPTED,
            safe_error=pin_error,
            expected_status=TurnStatus.PENDING,
        )
        assert interrupted.execution_state == TurnStatus.INTERRUPTED.value


def _pending(
    sessions: sessionmaker[Session], key: str
) -> tuple[AuthorizationContext, str]:
    authorization = _auth(f"tenant_{key}", f"principal_{key}", "ecommerce.read")
    conversation_id = f"conv_{key}"
    turn_id = f"turn_{key}"
    with sessions() as session:
        repository = V2Repository(session)
        repository.create_conversation(
            authorization,
            conversation_id=conversation_id,
            mode=ConversationMode.SHOPPER,
        )
        repository.record_turn(
            authorization,
            conversation_id=conversation_id,
            turn_id=turn_id,
            client_turn_id=f"client_{key}",
            payload={"message": f"synthetic lease test {key}"},
        )
    return authorization, turn_id


def _claim(
    sessions: sessionmaker[Session],
    authorization: AuthorizationContext,
    turn_id: str,
    lease_owner: str,
) -> None:
    with sessions() as session:
        V2Repository(session).claim_turn(
            authorization,
            turn_id,
            lease_owner=lease_owner,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )


def _persist_step(
    repository: V2Repository,
    authorization: AuthorizationContext,
    turn_id: str,
    lease_owner: str | None,
    value: str,
) -> V2StepResult:
    return repository.persist_step_result(
        authorization,
        turn_id=turn_id,
        step_result_id=f"result_{turn_id}",
        operation_key=_operation_key(turn_id),
        status=TaskStatus.SUCCESS,
        result={"value": value},
        data_version="synthetic_v1",
        lease_owner=lease_owner,
    )


def _complete_answered(
    repository: V2Repository,
    authorization: AuthorizationContext,
    turn_id: str,
    *,
    lease_owner: str | None = None,
    expected_status: TurnStatus | None = None,
    answer: str = "accepted answer",
) -> V2Turn:
    return repository.complete_turn(
        authorization,
        turn_id,
        status=TurnStatus.COMPLETED,
        dialogue_outcome=DialogueOutcome.ANSWERED,
        result={"answer": answer},
        lease_owner=lease_owner,
        expected_status=expected_status,
    )


def _set_lease_expiry(
    sessions: sessionmaker[Session],
    turn_id: str,
    expiry: datetime | None,
) -> None:
    with sessions() as session:
        session.execute(
            update(V2Turn).where(V2Turn.id == turn_id).values(lease_expires_at=expiry)
        )
        session.commit()


def _operation_key(turn_id: str) -> str:
    return f"catalog.search:sha256:{turn_id}"


def _auth(tenant_id: str, principal_id: str, *scopes: str) -> AuthorizationContext:
    return AuthorizationContext(
        tenant_id=tenant_id,
        principal_id=principal_id,
        scopes=frozenset(scopes),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value
