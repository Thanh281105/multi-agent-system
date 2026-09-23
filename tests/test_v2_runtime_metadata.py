"""Real persistence coverage for snapshot pins and partial runtime accounting."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext
from app.db.migrate import (
    EXPECTED_DATABASE_REVISION,
    _migration_config,
    check_database_schema,
    upgrade_database,
)
from app.db.v2_repository import TurnRuntimeConflictError, V2Repository
from app.models.v2 import V2KnowledgeCorpusVersion, V2Turn
from app.v2.authorization import AuthorizationDeniedError, ResourceNotFoundError
from app.v2.contracts import ConversationMode, SafeExecutionError, TurnStatus
from tests.v2_postgres_support import disposable_postgres_database

_CORPUS = "cor_runtime_metadata_fixture"
_VERSIONS = {
    "catalog_version_id": "cat_runtime_fixture",
    "corpus_version_id": _CORPUS,
    "index_manifest_id": "idx_runtime_fixture",
}
_LEASE_OWNER = "runtime_metadata_test_worker"


@pytest.fixture(scope="module")
def sessions() -> Iterator[sessionmaker[Session]]:
    with disposable_postgres_database("thanh_v2_p2_runtime_meta_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
        try:
            with factory() as session:
                session.add(
                    V2KnowledgeCorpusVersion(
                        id=_CORPUS,
                        corpus_name="Runtime metadata fixture",
                        version="1",
                        status="published",
                        manifest={},
                    )
                )
                session.commit()
            yield factory
        finally:
            engine.dispose()


def test_sqlite_migration_preserves_old_failed_turn_and_refuses_lossy_downgrade(
    tmp_path: Path,
) -> None:
    _exercise_migration(f"sqlite+pysqlite:///{(tmp_path / 'runtime.db').as_posix()}")


def test_postgres_migration_preserves_old_failed_turn_and_refuses_lossy_downgrade() -> (
    None
):
    with disposable_postgres_database("thanh_v2_p2_runtime_migrate_") as database_url:
        _exercise_migration(database_url)


def test_binding_is_immutable_separate_from_client_retry_identity(
    sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(sessions)
    with sessions() as session:
        repository = V2Repository(session)
        initial = repository.get_turn(authorization, turn_id)
        original_hash = initial.request_payload_hash
        original_payload = initial.request_payload.copy()
        repository.bind_turn_runtime(authorization, turn_id, data_versions=_VERSIONS)
        repository.bind_turn_runtime(authorization, turn_id, data_versions=_VERSIONS)
        replay = repository.record_turn(
            authorization,
            conversation_id=initial.conversation_id,
            turn_id=f"turn_{uuid4().hex}",
            client_turn_id=initial.client_turn_id,
            payload=original_payload,
            corpus_version_id=None,
        )
        assert replay.id == turn_id
        assert replay.request_payload_hash == original_hash
        assert replay.corpus_version_id == _CORPUS
        assert replay.runtime_metadata == _metadata()
        with pytest.raises(TurnRuntimeConflictError, match="already bound"):
            repository.bind_turn_runtime(
                authorization,
                turn_id,
                data_versions={**_VERSIONS, "catalog_version_id": "cat_different"},
            )
        with pytest.raises(TurnRuntimeConflictError, match="corpus pin"):
            repository.bind_turn_runtime(
                authorization,
                turn_id,
                data_versions={**_VERSIONS, "corpus_version_id": "cor_different"},
            )
    with sessions() as session:
        assert session.get(V2Turn, turn_id).runtime_metadata == _metadata()


def test_concurrent_different_runtime_bindings_have_one_winner(
    sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(sessions)
    barrier = Barrier(2)

    def bind(index: int) -> str:
        with sessions() as session:
            barrier.wait(timeout=10)
            try:
                V2Repository(session).bind_turn_runtime(
                    authorization,
                    turn_id,
                    data_versions={
                        **_VERSIONS,
                        "index_manifest_id": f"idx_candidate_{index}",
                    },
                )
                return f"idx_candidate_{index}"
            except TurnRuntimeConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = tuple(workers.map(bind, (1, 2)))
    assert outcomes.count("conflict") == 1
    winner = next(item for item in outcomes if item != "conflict")
    with sessions() as session:
        saved = session.get(V2Turn, turn_id).runtime_metadata
        assert saved["data_versions"]["index_manifest_id"] == winner


@pytest.mark.parametrize(
    "status", [TurnStatus.FAILED, TurnStatus.CANCELLED, TurnStatus.INTERRUPTED]
)
def test_attempt_counters_survive_terminal_failure_and_new_sessions(
    sessions: sessionmaker[Session], status: TurnStatus
) -> None:
    authorization, turn_id = _pending(sessions)
    _claim(sessions, authorization, turn_id)
    with sessions() as session:
        repository = V2Repository(session)
        repository.checkpoint_turn_runtime(
            authorization, turn_id, lease_owner=_LEASE_OWNER, knowledge_retrievals=1
        )
        repository.checkpoint_turn_runtime(
            authorization,
            turn_id,
            lease_owner=_LEASE_OWNER,
            knowledge_retrievals=2,
            draft_repairs=1,
        )
    # Recreate the repository/session as on retry after a worker stops. The
    # checkpoint exists even though no successful answer was ever persisted.
    with sessions() as session:
        repository = V2Repository(session)
        assert repository.get_turn(
            authorization, turn_id
        ).runtime_metadata == _metadata(2, 1)
        error = (
            None
            if status is TurnStatus.CANCELLED
            else SafeExecutionError(code="model_timeout", message="Model timed out.")
        )
        terminal = repository.complete_turn(
            authorization, turn_id, status=status, safe_error=error
        )
        assert terminal.result is None
    with sessions() as session:
        repository = V2Repository(session)
        saved = repository.get_turn(authorization, turn_id)
        assert saved.execution_state == status.value
        assert saved.runtime_metadata == _metadata(2, 1)
        with pytest.raises(TurnRuntimeConflictError, match="lease"):
            repository.checkpoint_turn_runtime(
                authorization, turn_id, lease_owner=_LEASE_OWNER, draft_repairs=1
            )


def test_partial_checkpoints_are_monotonic_and_do_not_clobber_other_counter(
    sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(sessions)
    _claim(sessions, authorization, turn_id)
    barrier = Barrier(2)

    def checkpoint(field: str) -> None:
        with sessions() as session:
            barrier.wait(timeout=10)
            V2Repository(session).checkpoint_turn_runtime(
                authorization, turn_id, lease_owner=_LEASE_OWNER, **{field: 1}
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        tuple(workers.map(checkpoint, ("knowledge_retrievals", "draft_repairs")))
    with sessions() as session:
        repository = V2Repository(session)
        assert repository.get_turn(
            authorization, turn_id
        ).runtime_metadata == _metadata(1, 1)
        with pytest.raises(TurnRuntimeConflictError, match="decrease"):
            repository.checkpoint_turn_runtime(
                authorization, turn_id, lease_owner=_LEASE_OWNER, knowledge_retrievals=0
            )
        replay = repository.checkpoint_turn_runtime(
            authorization, turn_id, lease_owner=_LEASE_OWNER, knowledge_retrievals=1
        )
        assert replay.runtime_metadata == _metadata(1, 1)


@pytest.mark.parametrize("value", [-1, 3, True, "1", 1.0])
def test_checkpoint_rejects_out_of_range_or_coerced_counters(
    sessions: sessionmaker[Session], value: object
) -> None:
    authorization, turn_id = _pending(sessions)
    _claim(sessions, authorization, turn_id)
    with sessions() as session:
        repository = V2Repository(session)
        with pytest.raises(ValueError, match="strict integer"):
            repository.checkpoint_turn_runtime(
                authorization,
                turn_id,
                lease_owner=_LEASE_OWNER,
                knowledge_retrievals=value,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="strict integer"):
            repository.checkpoint_turn_runtime(
                authorization, turn_id, lease_owner=_LEASE_OWNER, draft_repairs=2
            )
        assert (
            repository.get_turn(authorization, turn_id).runtime_metadata == _metadata()
        )


def test_bind_and_checkpoint_recheck_owner_scope_and_live_lease(
    sessions: sessionmaker[Session],
) -> None:
    authorization, turn_id = _pending(sessions)
    foreign = AuthorizationContext(
        tenant_id=authorization.tenant_id,
        principal_id="someone_else",
        scopes=frozenset({"ecommerce.read"}),
    )
    revoked = authorization.model_copy(update={"scopes": frozenset()})
    with sessions() as session:
        repository = V2Repository(session)
        for credential, exception in (
            (foreign, ResourceNotFoundError),
            (revoked, AuthorizationDeniedError),
        ):
            with pytest.raises(exception):
                repository.bind_turn_runtime(
                    credential, turn_id, data_versions=_VERSIONS
                )
    _claim(sessions, authorization, turn_id)
    with sessions() as session:
        repository = V2Repository(session)
        for credential, exception in (
            (foreign, ResourceNotFoundError),
            (revoked, AuthorizationDeniedError),
        ):
            with pytest.raises(exception):
                repository.checkpoint_turn_runtime(
                    credential, turn_id, lease_owner=_LEASE_OWNER, draft_repairs=1
                )
        with pytest.raises(TurnRuntimeConflictError, match="lease"):
            repository.checkpoint_turn_runtime(
                authorization, turn_id, lease_owner="wrong_worker", draft_repairs=1
            )
        with pytest.raises(TurnRuntimeConflictError, match="pending"):
            repository.bind_turn_runtime(
                authorization, turn_id, data_versions=_VERSIONS
            )
        row = session.get(V2Turn, turn_id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        with pytest.raises(TurnRuntimeConflictError, match="lease"):
            repository.checkpoint_turn_runtime(
                authorization, turn_id, lease_owner=_LEASE_OWNER, draft_repairs=1
            )
        assert (
            repository.get_turn(authorization, turn_id).runtime_metadata == _metadata()
        )


def _pending(
    factory: sessionmaker[Session],
) -> tuple[AuthorizationContext, str]:
    key = uuid4().hex
    authorization = AuthorizationContext(
        tenant_id="runtime_metadata_tenant",
        principal_id=f"principal_{key}",
        scopes=frozenset({"ecommerce.read"}),
    )
    turn_id = f"turn_{key}"
    with factory() as session:
        repository = V2Repository(session)
        conversation = repository.create_conversation(
            authorization, conversation_id=f"conv_{key}", mode=ConversationMode.SHOPPER
        )
        repository.record_turn(
            authorization,
            conversation_id=conversation.id,
            turn_id=turn_id,
            client_turn_id=f"client-{key}",
            payload={"message": "Synthetic read"},
            corpus_version_id=_CORPUS,
        )
    return authorization, turn_id


def _claim(
    factory: sessionmaker[Session], authorization: AuthorizationContext, turn_id: str
) -> None:
    with factory() as session:
        repository = V2Repository(session)
        repository.bind_turn_runtime(authorization, turn_id, data_versions=_VERSIONS)
        repository.claim_turn(
            authorization,
            turn_id,
            lease_owner=_LEASE_OWNER,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )


def _metadata(knowledge: int = 0, repairs: int = 0) -> dict[str, object]:
    return {
        "schema_version": 1,
        "data_versions": _VERSIONS.copy(),
        "knowledge_retrievals": knowledge,
        "draft_repairs": repairs,
    }


def _exercise_migration(database_url: str) -> None:
    upgrade_database(database_url, revision="20260909_0006")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO v2_conversations "
                    "(id, tenant_id, principal_id, mode, store_id) VALUES "
                    "('conv_legacy', 'legacy_tenant', 'legacy_principal', "
                    "'shopper', 'demo')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO v2_turns (id, conversation_id, tenant_id, "
                    "principal_id, mode, store_id, client_turn_id, "
                    "request_payload_hash, request_payload, "
                    "execution_state, safe_error, completed_at) VALUES "
                    "('turn_legacy', 'conv_legacy', 'legacy_tenant', "
                    "'legacy_principal', "
                    "'shopper', 'demo', 'client-legacy', :payload_hash, :payload, "
                    "'failed', :safe_error, CURRENT_TIMESTAMP)"
                ),
                {
                    "payload_hash": "a" * 64,
                    "payload": '{"message":"old read"}',
                    "safe_error": '{"code":"old","message":"old","retryable":false}',
                },
            )
            before = connection.execute(
                text(
                    "SELECT request_payload_hash, request_payload, execution_state, "
                    "safe_error, completed_at FROM v2_turns"
                )
            ).one()
        upgrade_database(database_url)
        upgrade_database(database_url)
        check_database_schema(database_url)
        with Session(engine) as session:
            assert session.get(V2Turn, "turn_legacy").runtime_metadata == {}
        _downgrade(engine)
        assert "runtime_metadata" not in {
            column["name"] for column in inspect(engine).get_columns("v2_turns")
        }
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT request_payload_hash, request_payload, "
                        "execution_state, "
                        "safe_error, completed_at FROM v2_turns"
                    )
                ).one()
                == before
            )
        upgrade_database(database_url)
        with Session(engine) as session:
            row = session.get(V2Turn, "turn_legacy")
            row.runtime_metadata = _metadata(1, 1)
            session.commit()
        with pytest.raises(RuntimeError, match="would be lost"):
            _downgrade(engine)
        with Session(engine) as session:
            assert session.get(V2Turn, "turn_legacy").runtime_metadata == _metadata(
                1, 1
            )
            assert (
                session.scalar(text("SELECT version_num FROM alembic_version"))
                == EXPECTED_DATABASE_REVISION
            )
    finally:
        engine.dispose()


def _downgrade(engine: Engine) -> None:
    migration_config = _migration_config()
    with engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        command.downgrade(migration_config, "20260909_0006")
