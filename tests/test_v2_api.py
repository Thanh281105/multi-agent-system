"""Focused transport tests for the authenticated v2 JSON surface."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext
from app.core.config import Settings
from app.db import session as db_session
from app.db.migrate import upgrade_database
from app.db.v2_repository import V2Repository
from app.main import create_app
from app.models.v2 import V2Turn
from app.v2.actions import ActionIdempotencyConflictError, StoredAction
from app.v2.authorization import ResourceNotFoundError, bind_request_authorization
from app.v2.contracts import (
    ActionCard,
    ActionChange,
    ActionConfirmRequest,
    ActionDecisionResponse,
    ActionKind,
    ActionStatus,
    ActionTarget,
    ChatRequest,
    ConversationMode,
    DialogueOutcome,
    TurnResult,
    TurnStatus,
)
from app.v2.execution import DurableTurnOutcome
from app.v2.planning import PlanningContext, RuntimeDataVersions
from app.v2.registry import ActionExecutionResult
from app.v2.runtime import (
    ResolvedV2Runtime,
    V2RuntimeConfigurationError,
    V2RuntimeFactory,
    V2ServiceGraph,
)
from tests.v2_postgres_support import disposable_postgres_database

ALICE_HEADERS = {"X-API-Key": "alice-secret-key"}


class _MissingResourceFactory:
    initialized = False

    def resolve_for_request(
        self,
        authorization: AuthorizationContext,
        *,
        mode: ConversationMode,
    ) -> object:
        bind_request_authorization(authorization, mode)
        raise ResourceNotFoundError

    def resolve_for_conversation(self, *_args: object, **_kwargs: object) -> object:
        raise ResourceNotFoundError

    def resolve_for_turn(self, *_args: object, **_kwargs: object) -> object:
        raise ResourceNotFoundError

    def resolve_for_action(self, *_args: object, **_kwargs: object) -> object:
        raise ResourceNotFoundError


class _UnavailableFactory(_MissingResourceFactory):
    def resolve_for_conversation(self, *_args: object, **_kwargs: object) -> object:
        raise V2RuntimeConfigurationError("unavailable", "sensitive detail")


class _FakeLedger:
    def __init__(self) -> None:
        self.scope_ids: list[str] = []

    def create_scope(self, *, scope_id: str, **_kwargs: object) -> None:
        if scope_id not in self.scope_ids:
            self.scope_ids.append(scope_id)


class _FakeTurnService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.provider_runs = 0

    async def execute(
        self,
        request: ChatRequest,
        context: PlanningContext,
        **_kwargs: object,
    ) -> DurableTurnOutcome:
        from app.db.v2_repository import canonical_turn_id

        turn_id = canonical_turn_id(request.conversation_id, request.client_turn_id)
        with self.sessions() as session:
            existing = session.get(V2Turn, turn_id)
            if existing is None:
                self.provider_runs += 1
                result = TurnResult(
                    outcome=DialogueOutcome.ANSWERED,
                    answer="Câu trả lời đã kiểm chứng.",
                )
                binding = context.access.binding
                now = datetime.now(UTC)
                session.add(
                    V2Turn(
                        id=turn_id,
                        conversation_id=request.conversation_id,
                        tenant_id=binding.tenant_id,
                        principal_id=binding.principal_id,
                        mode=binding.mode.value,
                        store_id=binding.store_id,
                        client_turn_id=request.client_turn_id,
                        request_payload_hash=hashlib.sha256(
                            request.message.encode()
                        ).hexdigest(),
                        request_payload={"message": request.message},
                        execution_state=TurnStatus.COMPLETED.value,
                        dialogue_outcome=DialogueOutcome.ANSWERED.value,
                        result=result.model_dump(mode="json"),
                        runtime_metadata={},
                        created_at=now,
                        completed_at=now,
                        updated_at=now,
                    )
                )
                session.commit()
                reused = False
            else:
                result = TurnResult.model_validate(existing.result)
                reused = True
        return DurableTurnOutcome(
            turn_id=turn_id,
            status=TurnStatus.COMPLETED,
            outcome=DialogueOutcome.ANSWERED,
            result=result,
            reused=reused,
        )

    def query(
        self, turn_id: str, *_args: object, **_kwargs: object
    ) -> DurableTurnOutcome:
        with self.sessions() as session:
            row = session.get(V2Turn, turn_id)
            if row is None:
                raise ResourceNotFoundError
            result = TurnResult.model_validate(row.result)
        return DurableTurnOutcome(
            turn_id=turn_id,
            status=TurnStatus.COMPLETED,
            outcome=DialogueOutcome.ANSWERED,
            result=result,
            reused=True,
        )

    async def cancel(
        self,
        turn_id: str,
        *_args: object,
        **_kwargs: object,
    ) -> DurableTurnOutcome:
        return self.query(turn_id)


class _TerminalRowRunningOutcomeService(_FakeTurnService):
    async def execute(
        self,
        request: ChatRequest,
        context: PlanningContext,
        **kwargs: object,
    ) -> DurableTurnOutcome:
        completed = await super().execute(request, context, **kwargs)
        return DurableTurnOutcome(
            turn_id=completed.turn_id,
            status=TurnStatus.RUNNING,
            reused=True,
        )


class _FakeActionService:
    def __init__(self) -> None:
        self.confirm_keys: list[str] = []
        self.confirmed_versions: dict[str, int] = {}
        self.card = _action_card()

    def read_action_result(self, *_args: object, **_kwargs: object) -> StoredAction:
        return StoredAction(card=self.card, result=None)

    def confirm_action(
        self,
        *_args: object,
        idempotency_key: str,
        request: ActionConfirmRequest,
        **_kwargs: object,
    ) -> ActionExecutionResult:
        previous_version = self.confirmed_versions.get(idempotency_key)
        if (
            previous_version is not None
            and previous_version != request.proposal_version
        ):
            raise ActionIdempotencyConflictError("payload differs")
        reused = previous_version is not None
        self.confirmed_versions[idempotency_key] = request.proposal_version
        self.confirm_keys.append(idempotency_key)
        return ActionExecutionResult(
            action_id=self.card.action_id,
            status=ActionStatus.EXECUTED,
            resource_id="order_demo",
            resource_version=2,
            reused_result=reused,
        )

    def reject_action(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> ActionDecisionResponse:
        return ActionDecisionResponse(
            action_id=self.card.action_id,
            proposal_version=1,
            status=ActionStatus.REJECTED,
            decided_at=datetime.now(UTC),
        )


class _SurfaceFactory(V2RuntimeFactory):
    action_ids = frozenset({"action_checkout"})

    def resolve_for_action(
        self,
        authorization: AuthorizationContext,
        action_id: str,
        *,
        write: bool = False,
    ) -> ResolvedV2Runtime:
        if action_id not in self.action_ids:
            raise ResourceNotFoundError
        access = bind_request_authorization(
            authorization,
            ConversationMode.SHOPPER,
            write=write,
        )
        services = self._resolve_services()
        return ResolvedV2Runtime(
            access=access,
            planning_context=PlanningContext(
                access=access,
                versions=services.versions,
            ),
            services=services,
        )


def test_openapi_mounts_complete_json_surface_and_keeps_factory_lazy() -> None:
    application = create_app(_settings())
    factory = application.state.gateway_runtime.v2_runtime_factory

    paths = application.openapi()["paths"]
    expected = {
        "/api/v2/me": {"get"},
        "/api/v2/conversations": {"get", "post"},
        "/api/v2/conversations/{conversation_id}": {"get", "delete"},
        "/api/v2/chat": {"post"},
        "/api/v2/chat/stream": {"post"},
        "/api/v2/turns/{turn_id}": {"get"},
        "/api/v2/turns/{turn_id}/cancel": {"post"},
        "/api/v2/actions/{action_id}": {"get"},
        "/api/v2/actions/{action_id}/confirm": {"post"},
        "/api/v2/actions/{action_id}/reject": {"post"},
        "/api/v2/memory": {"get", "put", "delete"},
    }
    assert {
        path: set(paths[path]).intersection({"get", "post", "put", "delete"})
        for path in expected
    } == expected
    schemas = application.openapi()["components"]["schemas"]
    assert tuple(schemas["GatewayChatRequest"]["properties"]) == (
        "message",
        "session_id",
    )
    assert tuple(schemas["GatewayChatResponse"]["properties"]) == (
        "api_version",
        "status",
        "answer",
        "session_id",
        "request_id",
        "trace_id",
        "intent",
        "active_agent",
        "selected_product_id",
        "executions",
        "provenance",
        "model_calls",
        "warnings",
        "sample_data",
        "duration_ms",
    )
    assert factory.initialized is False


def test_me_uses_authenticated_server_policy_only() -> None:
    response = TestClient(create_app(_settings())).get(
        "/api/v2/me",
        headers=ALICE_HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {
        "principal_id": "alice",
        "tenant_id": "tenant-alice",
        "allowed_modes": ["shopper"],
        "store_id": "demo",
    }


def test_conversation_create_list_and_rich_detail_surface() -> None:
    factory, _, _ = _surface_factory()
    client = TestClient(create_app(_settings(), v2_runtime_factory=factory))

    created = client.post(
        "/api/v2/conversations",
        headers=ALICE_HEADERS,
        json={"mode": "shopper"},
    )
    conversation_id = created.json()["conversation"]["conversation_id"]
    listed = client.get(
        "/api/v2/conversations?mode=shopper",
        headers=ALICE_HEADERS,
    )
    detail = client.get(
        f"/api/v2/conversations/{conversation_id}",
        headers=ALICE_HEADERS,
    )

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()["conversations"][0]["conversation_id"] == conversation_id
    assert detail.status_code == 200
    assert detail.json()["conversation"]["mode"] == "shopper"
    assert detail.json()["turns"] == []


def test_chat_retry_get_and_cancel_use_one_durable_turn() -> None:
    factory, turns, ledger = _surface_factory()
    client = TestClient(create_app(_settings(), v2_runtime_factory=factory))
    created = client.post(
        "/api/v2/conversations",
        headers=ALICE_HEADERS,
        json={"mode": "shopper"},
    )
    conversation_id = created.json()["conversation"]["conversation_id"]
    payload = {
        "conversation_id": conversation_id,
        "client_turn_id": "client-retry-1",
        "message": "Gợi ý sách lịch sử",
    }

    first = client.post(
        "/api/v2/chat",
        headers={**ALICE_HEADERS, "X-Request-ID": "req_v2_chat_001"},
        json=payload,
    )
    replay = client.post("/api/v2/chat", headers=ALICE_HEADERS, json=payload)
    turn_id = first.json()["turn"]["turn_id"]
    queried = client.get(f"/api/v2/turns/{turn_id}", headers=ALICE_HEADERS)
    cancelled = client.post(
        f"/api/v2/turns/{turn_id}/cancel",
        headers=ALICE_HEADERS,
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["request_id"] == "req_v2_chat_001"
    assert first.json()["trace_id"].startswith("trace_")
    assert first.json()["result"]["answer"] == "Câu trả lời đã kiểm chứng."
    assert replay.json()["turn"]["turn_id"] == turn_id
    assert queried.status_code == cancelled.status_code == 200
    assert turns.provider_runs == 1
    assert len(ledger.scope_ids) == 1


def test_chat_projects_http_status_and_body_from_one_outcome_during_race() -> None:
    factory, turns, _ = _surface_factory()
    racing = _TerminalRowRunningOutcomeService(turns.sessions)
    factory._services.turn_service = racing  # type: ignore[union-attr]  # noqa: SLF001
    client = TestClient(create_app(_settings(), v2_runtime_factory=factory))
    created = client.post(
        "/api/v2/conversations",
        headers=ALICE_HEADERS,
        json={"mode": "shopper"},
    )
    payload = {
        "conversation_id": created.json()["conversation"]["conversation_id"],
        "client_turn_id": "client-race-1",
        "message": "Kiểm tra trạng thái đồng nhất",
    }

    raced = client.post("/api/v2/chat", headers=ALICE_HEADERS, json=payload)
    queried = client.get(
        f"/api/v2/turns/{raced.json()['turn']['turn_id']}",
        headers=ALICE_HEADERS,
    )

    assert raced.status_code == 202
    assert raced.json()["turn"]["status"] == "running"
    assert raced.json()["turn"]["outcome"] is None
    assert raced.json()["turn"]["completed_at"] is None
    assert raced.json()["result"] is None
    assert raced.json()["error"] is None
    assert queried.status_code == 200
    assert queried.json()["turn"]["status"] == "completed"


def test_action_read_confirm_header_and_reject_projection() -> None:
    factory, _, _ = _surface_factory(write=True)
    actions = cast(_FakeActionService, factory._services.action_service)  # noqa: SLF001
    client = TestClient(
        create_app(
            _settings(policy="alice:tenant-alice:ecommerce.read|ecommerce.write"),
            v2_runtime_factory=factory,
        )
    )

    read = client.get("/api/v2/actions/action_checkout", headers=ALICE_HEADERS)
    missing_header = client.post(
        "/api/v2/actions/action_checkout/confirm",
        headers=ALICE_HEADERS,
        json={"proposal_version": 1},
    )
    confirmed = client.post(
        "/api/v2/actions/action_checkout/confirm",
        headers={**ALICE_HEADERS, "Idempotency-Key": "confirm-key-0001"},
        json={"proposal_version": 1},
    )
    replayed = client.post(
        "/api/v2/actions/action_checkout/confirm",
        headers={**ALICE_HEADERS, "Idempotency-Key": "confirm-key-0001"},
        json={"proposal_version": 1},
    )
    conflict = client.post(
        "/api/v2/actions/action_checkout/confirm",
        headers={**ALICE_HEADERS, "Idempotency-Key": "confirm-key-0001"},
        json={"proposal_version": 2},
    )
    rejected = client.post(
        "/api/v2/actions/action_checkout/reject",
        headers=ALICE_HEADERS,
        json={"proposal_version": 1, "reason": "Không mua nữa"},
    )

    assert read.status_code == 200
    assert read.json()["action"]["action_id"] == "action_checkout"
    assert read.json()["result"] is None
    assert missing_header.status_code == 422
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "executed"
    assert replayed.status_code == 200
    assert replayed.json()["reused_result"] is True
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "v2.idempotency_conflict"
    assert actions.confirm_keys == ["confirm-key-0001", "confirm-key-0001"]
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_v2_auth_and_validation_errors_are_safe_and_correlated() -> None:
    client = TestClient(create_app(_settings()))

    unauthenticated = client.get("/api/v2/me")
    invalid = client.post(
        "/api/v2/chat",
        headers=ALICE_HEADERS,
        json={
            "conversation_id": "conversation_safe",
            "client_turn_id": "client-1",
            "message": "x" * 2_001,
            "authorization": {"scopes": ["merchant.write"]},
        },
    )
    injected_authority = client.post(
        "/api/v2/chat",
        headers=ALICE_HEADERS,
        json={
            "conversation_id": "conversation_safe",
            "client_turn_id": "client-authority-1",
            "message": "Yêu cầu hợp lệ",
            "authorization": {"scopes": ["merchant.write"]},
        },
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["request_id"].startswith("req_")
    assert unauthenticated.json()["error"]["trace_id"].startswith("trace_")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "v2.validation_failed"
    assert "merchant.write" not in invalid.text
    assert invalid.json()["error"]["request_id"].startswith("req_")
    assert injected_authority.status_code == 422
    assert injected_authority.json()["error"]["code"] == "v2.validation_failed"
    assert "merchant.write" not in injected_authority.text


def test_missing_header_and_foreign_resources_have_stable_statuses() -> None:
    factory = cast(V2RuntimeFactory, _MissingResourceFactory())
    client = TestClient(create_app(_settings(), v2_runtime_factory=factory))

    header_error = client.post(
        "/api/v2/actions/action_missing/confirm",
        headers=ALICE_HEADERS,
        json={"proposal_version": 1},
    )
    missing = client.get(
        "/api/v2/conversations/conversation_missing",
        headers=ALICE_HEADERS,
    )
    revoked = client.get(
        "/api/v2/conversations?mode=merchant",
        headers=ALICE_HEADERS,
    )
    missing_stream = client.post(
        "/api/v2/chat/stream",
        headers=ALICE_HEADERS,
        json={
            "conversation_id": "conversation_missing",
            "client_turn_id": "client-missing-stream",
            "message": "Không được nhìn thấy",
        },
    )
    missing_turn = client.get(
        "/api/v2/turns/turn_missing",
        headers=ALICE_HEADERS,
    )
    missing_action = client.get(
        "/api/v2/actions/action_missing",
        headers=ALICE_HEADERS,
    )

    assert header_error.status_code == 422
    assert header_error.json()["error"]["code"] == "v2.validation_failed"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "v2.resource_not_found"
    assert revoked.status_code == 403
    assert revoked.json()["error"]["code"] == "v2.forbidden"
    assert missing_stream.status_code == 404
    assert missing_stream.json()["error"]["code"] == "v2.resource_not_found"
    assert missing_turn.status_code == 404
    assert missing_turn.json()["error"]["code"] == "v2.resource_not_found"
    assert missing_action.status_code == 404
    assert missing_action.json()["error"]["code"] == "v2.resource_not_found"


def test_runtime_failures_are_sanitized_retryable_503() -> None:
    factory = cast(V2RuntimeFactory, _UnavailableFactory())
    response = TestClient(create_app(_settings(), v2_runtime_factory=factory)).get(
        "/api/v2/conversations/conversation_safe",
        headers=ALICE_HEADERS,
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "v2.runtime_unavailable"
    assert response.json()["error"]["retryable"] is True
    assert "sensitive detail" not in response.text


def test_postgres_owner_isolation_and_delete_semantics() -> None:
    with disposable_postgres_database("thanh_v2_p2_api_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
        owner = AuthorizationContext(
            principal_id="alice",
            tenant_id="tenant-alice",
            scopes=frozenset({"ecommerce.read", "ecommerce.write"}),
        )
        foreign = AuthorizationContext(
            principal_id="bob",
            tenant_id="tenant-bob",
            scopes=frozenset({"ecommerce.read"}),
        )
        try:
            with sessions() as session:
                repository = V2Repository(session)
                repository.create_conversation(
                    owner,
                    conversation_id="conversation_alice",
                    mode=ConversationMode.SHOPPER,
                )
                repository.create_conversation(
                    foreign,
                    conversation_id="conversation_bob",
                    mode=ConversationMode.SHOPPER,
                )
                _seed_completed_turn(
                    session,
                    conversation_id="conversation_alice",
                    authorization=owner,
                    turn_id="turn_alice",
                )
                _seed_completed_turn(
                    session,
                    conversation_id="conversation_bob",
                    authorization=foreign,
                    turn_id="turn_bob",
                )
                session.commit()
            factory = V2RuntimeFactory(_settings(), session_factory=sessions)
            factory._services = cast(  # noqa: SLF001 - isolated injected graph
                V2ServiceGraph,
                SimpleNamespace(
                    session_factory=sessions,
                    versions=RuntimeDataVersions(
                        catalog_version_id="catalog_test",
                        corpus_version_id="corpus_test",
                        index_manifest_id="index_test",
                    ),
                ),
            )
            client = TestClient(
                create_app(
                    _settings(
                        policy=("alice:tenant-alice:ecommerce.read|ecommerce.write")
                    ),
                    v2_runtime_factory=factory,
                )
            )

            own = client.get(
                "/api/v2/conversations/conversation_alice",
                headers=ALICE_HEADERS,
            )
            hidden = client.get(
                "/api/v2/conversations/conversation_bob",
                headers=ALICE_HEADERS,
            )
            remembered = client.put(
                "/api/v2/memory",
                headers=ALICE_HEADERS,
                json={
                    "source_turn_id": "turn_alice",
                    "preference": {"kind": "genre", "value": "Lịch sử"},
                },
            )
            foreign_source = client.put(
                "/api/v2/memory",
                headers=ALICE_HEADERS,
                json={
                    "source_turn_id": "turn_bob",
                    "preference": {"kind": "genre", "value": "Sai chủ sở hữu"},
                },
            )
            memory = client.get(
                "/api/v2/memory?mode=shopper",
                headers=ALICE_HEADERS,
            )
            forgotten = client.request(
                "DELETE",
                "/api/v2/memory?mode=shopper",
                headers=ALICE_HEADERS,
                json={"preference_id": remembered.json()["preference_id"]},
            )
            empty_memory = client.get(
                "/api/v2/memory?mode=shopper",
                headers=ALICE_HEADERS,
            )
            deleted = client.delete(
                "/api/v2/conversations/conversation_alice",
                headers=ALICE_HEADERS,
            )
            after_delete = client.get(
                "/api/v2/conversations/conversation_alice",
                headers=ALICE_HEADERS,
            )

            assert own.status_code == 200
            assert hidden.status_code == 404
            assert remembered.status_code == 200
            assert remembered.json()["source_turn_id"] == "turn_alice"
            assert foreign_source.status_code == 404
            assert memory.status_code == 200
            assert len(memory.json()["preferences"]) == 1
            assert forgotten.status_code == 204
            assert empty_memory.json()["preferences"] == []
            assert deleted.status_code == 204
            assert deleted.content == b""
            assert after_delete.status_code == 404
        finally:
            engine.dispose()


def _surface_factory(
    *,
    write: bool = False,
) -> tuple[_SurfaceFactory, _FakeTurnService, _FakeLedger]:
    sessions = cast(sessionmaker[Session], db_session.SessionLocal)
    turns = _FakeTurnService(sessions)
    ledger = _FakeLedger()
    services = SimpleNamespace(
        session_factory=sessions,
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_test",
            corpus_version_id="corpus_test",
            index_manifest_id="index_test",
        ),
        turn_service=turns,
        action_service=_FakeActionService(),
        budget_ledger=ledger,
        budget_account_id="budget_test",
    )
    policy = "alice:tenant-alice:ecommerce.read"
    if write:
        policy += "|ecommerce.write"
    factory = _SurfaceFactory(_settings(policy=policy), session_factory=sessions)
    factory._services = cast(V2ServiceGraph, services)  # noqa: SLF001
    return factory, turns, ledger


def _seed_completed_turn(
    session: Session,
    *,
    conversation_id: str,
    authorization: AuthorizationContext,
    turn_id: str,
) -> None:
    now = datetime.now(UTC)
    result = TurnResult(
        outcome=DialogueOutcome.ANSWERED,
        answer="Lượt nguồn hợp lệ.",
    )
    session.add(
        V2Turn(
            id=turn_id,
            conversation_id=conversation_id,
            tenant_id=authorization.tenant_id,
            principal_id=authorization.principal_id,
            mode=ConversationMode.SHOPPER.value,
            store_id="demo",
            client_turn_id=f"client-{turn_id}",
            request_payload_hash=hashlib.sha256(turn_id.encode()).hexdigest(),
            request_payload={"message": "Ghi nhớ thể loại này"},
            execution_state=TurnStatus.COMPLETED.value,
            dialogue_outcome=DialogueOutcome.ANSWERED.value,
            result=result.model_dump(mode="json"),
            runtime_metadata={},
            created_at=now,
            completed_at=now,
            updated_at=now,
        )
    )


def _action_card() -> ActionCard:
    return ActionCard(
        action_id="action_checkout",
        proposal_id="proposal_checkout",
        proposal_version=1,
        kind=ActionKind.CHECKOUT,
        status=ActionStatus.PROPOSED,
        title="Xác nhận đơn hàng demo",
        required_permission="ecommerce.write",
        confirmation_required=True,
        target=ActionTarget(
            resource_type="cart",
            resource_id="cart_demo",
            expected_resource_version=1,
            data_version_ids=("catalog_test",),
        ),
        changes=(
            ActionChange(
                resource_type="order",
                resource_id="order_demo",
                field="status",
                after_text="created",
            ),
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _settings(
    *,
    policy: str = "alice:tenant-alice:ecommerce.read",
) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        gateway_api_keys="alice:alice-secret-key",
        gateway_principal_policies=policy,
        gateway_rate_limit_requests=50,
        gateway_rate_limit_window_seconds=60,
        legacy_chat_enabled=True,
    )
