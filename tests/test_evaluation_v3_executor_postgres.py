"""Real PostgreSQL proof for the Package 7 durable executor adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.migrate import upgrade_database
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_executor import EvaluationV3ObservationExecutor
from app.evaluation.v3_runner import (
    EvaluationCaseV3,
    EvaluationUserTurnV3,
    ObservationExecutionFailureV3,
    SandboxFixtureAdapterV3,
    _turn_request,
)
from app.models.budget import ProviderBudgetScope
from app.models.v2 import (
    V2Cart,
    V2CartLine,
    V2KnowledgeCorpusVersion,
    V2Offer,
    V2Order,
)
from app.shared.budget import (
    PricingManifest,
    ProviderUsage,
    SQLProviderBudgetLedger,
    current_provider_budget,
    default_pricing_manifest_path,
)
from app.v2.contracts import DialogueOutcome, TurnResult
from app.v2.execution import (
    DurableReadTurnExecutor,
    TurnComputation,
)
from app.v2.planning import RuntimeDataVersions
from app.v2.turn_service import V2TurnService
from tests.test_evaluation_v3_executor import _context_and_case
from tests.test_v2_tools import seed_tool_catalog
from tests.v2_postgres_support import disposable_postgres_database


class _SuccessfulHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def run_claimed(self, **_: Any) -> TurnComputation:
        self.calls += 1
        budget = current_provider_budget()
        assert budget is not None
        call_id = budget.new_call_id("generation")
        reservation = budget.ledger.reserve_attempt(
            scope_id=budget.scope_id,
            call_id=call_id,
            attempt_number=1,
            operation="generation",
            purpose=budget.purpose,
            model="gpt-5.4-mini-2026-03-17",
            request_fingerprint_sha256="d" * 64,
            input_token_bound=20,
            output_token_bound=10,
            attempt_timeout_seconds=budget.attempt_timeout_seconds,
        )
        assert budget.ledger.mark_dispatched(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
        )
        budget.ledger.settle_known_usage(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            usage=ProviderUsage(
                input_tokens=12,
                cached_input_tokens=2,
                output_tokens=4,
                reasoning_tokens=1,
                total_tokens=16,
            ),
            response_id="resp_p7_executor",
            response_model="gpt-5.4-mini-2026-03-17",
            response_service_tier="default",
        )
        budget.ledger.record_attempt_result(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            result_status="success",
        )
        return TurnComputation(
            result=TurnResult(
                outcome=DialogueOutcome.ANSWERED,
                answer="Authoritative durable result",
            )
        )


class _EmptyRecorder:
    def snapshot(self) -> tuple[()]:
        return ()


class _MutableRecorder:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def snapshot(self) -> tuple[Any, ...]:
        return tuple(self.calls)


class _ReleasedHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def run_claimed(self, **_: Any) -> TurnComputation:
        self.calls += 1
        budget = current_provider_budget()
        assert budget is not None
        reservation = budget.ledger.reserve_attempt(
            scope_id=budget.scope_id,
            call_id=budget.new_call_id("generation"),
            attempt_number=1,
            operation="generation",
            purpose=budget.purpose,
            model="gpt-5.4-mini-2026-03-17",
            request_fingerprint_sha256="e" * 64,
            input_token_bound=20,
            output_token_bound=10,
        )
        budget.ledger.abandon_undispatched(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            error_code="provider_dispatch_cancelled",
        )
        return TurnComputation(
            result=TurnResult(
                outcome=DialogueOutcome.ANSWERED,
                answer="Released before dispatch",
            )
        )


class _CancelledHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def run_claimed(self, **_: Any) -> TurnComputation:
        self.calls += 1
        budget = current_provider_budget()
        assert budget is not None
        reservation = budget.ledger.reserve_attempt(
            scope_id=budget.scope_id,
            call_id=budget.new_call_id("generation"),
            attempt_number=1,
            operation="generation",
            purpose=budget.purpose,
            model="gpt-5.4-mini-2026-03-17",
            request_fingerprint_sha256="f" * 64,
            input_token_bound=20,
            output_token_bound=10,
        )
        assert budget.ledger.mark_dispatched(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
        )
        budget.ledger.settle_unknown_usage(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            result_status="cancelled",
            error_code="provider_attempt_cancelled",
        )
        raise asyncio.CancelledError


class _RetrievalHandler:
    async def run_claimed(self, **_: Any) -> TurnComputation:
        return TurnComputation(
            result=TurnResult(
                outcome=DialogueOutcome.ANSWERED,
                answer="Unexpected retrieval",
            ),
            knowledge_retrievals=1,
        )


class _EmbeddingHandler:
    async def run_claimed(self, **_: Any) -> TurnComputation:
        budget = current_provider_budget()
        assert budget is not None
        reservation = budget.ledger.reserve_attempt(
            scope_id=budget.scope_id,
            call_id=budget.new_call_id("embedding"),
            attempt_number=1,
            operation="embedding",
            purpose=budget.purpose,
            model="text-embedding-3-small",
            request_fingerprint_sha256="1" * 64,
            input_token_bound=20,
            output_token_bound=0,
        )
        assert budget.ledger.mark_dispatched(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
        )
        budget.ledger.settle_known_usage(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            usage=ProviderUsage(
                input_tokens=5,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                total_tokens=5,
            ),
            response_id="resp_p7_embedding",
            response_model="text-embedding-3-small",
            response_service_tier=None,
        )
        budget.ledger.record_attempt_result(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            result_status="success",
        )
        return TurnComputation(
            result=TurnResult(
                outcome=DialogueOutcome.ANSWERED,
                answer="Unexpected embedding",
            )
        )


class _KnowledgePlanHandler:
    def __init__(self, recorder: _MutableRecorder) -> None:
        self.recorder = recorder

    async def run_claimed(self, **_: Any) -> TurnComputation:
        self.recorder.calls.append(SimpleNamespace(stage="knowledge_query_plan"))
        return TurnComputation(
            result=TurnResult(
                outcome=DialogueOutcome.ANSWERED,
                answer="Unexpected knowledge plan",
            )
        )


@dataclass(frozen=True, slots=True)
class _PostgresStore:
    engine: Any
    sessions: sessionmaker[Session]
    ledger: SQLProviderBudgetLedger
    versions: RuntimeDataVersions


@contextmanager
def _postgres_store(prefix: str) -> Iterator[_PostgresStore]:
    with disposable_postgres_database(prefix) as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=False,
            class_=Session,
        )
        ledger = SQLProviderBudgetLedger(
            sessions,
            PricingManifest.load(default_pricing_manifest_path()),
        )
        ledger.create_account(account_id="p7-executor-account")
        with sessions() as session:
            seed_tool_catalog(session)
        versions = RuntimeDataVersions(
            catalog_version_id="catalog_executor_v3",
            corpus_version_id="corpus_executor_v3",
            index_manifest_id="index_executor_v3",
        )
        with sessions() as session, session.begin():
            session.add(
                V2KnowledgeCorpusVersion(
                    id=versions.corpus_version_id,
                    corpus_name="p7-executor-test",
                    version="v3",
                    status="published",
                    manifest={},
                )
            )
        try:
            yield _PostgresStore(engine, sessions, ledger, versions)
        finally:
            engine.dispose()


def _runtime(
    store: _PostgresStore,
    handler: Any,
    context: Any,
    *,
    rag_enabled: bool = True,
    recorder: Any | None = None,
) -> Any:
    durable = DurableReadTurnExecutor(
        store.sessions,
        handler,
        budget_ledger=store.ledger,
        allowed_budget_purposes=frozenset({"warmup", "benchmark"}),
    )
    return SimpleNamespace(
        policy=SimpleNamespace(
            variant_id=context.identity.variant_id,
            rag_enabled=rag_enabled,
        ),
        shared_services=SimpleNamespace(
            session_factory=store.sessions,
            versions=store.versions,
            budget_ledger=store.ledger,
            budget_account_id="p7-executor-account",
        ),
        model_calls=recorder or _EmptyRecorder(),
        turn_service=V2TurnService(durable, worker_id="p7_executor_test"),
    )


@pytest.mark.asyncio
async def test_postgres_executor_resets_runs_and_replays_without_new_attempts() -> None:
    with disposable_postgres_database("thanh_v2_p2_p7exec_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        sessions = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=False,
            class_=Session,
        )
        ledger = SQLProviderBudgetLedger(
            sessions,
            PricingManifest.load(default_pricing_manifest_path()),
        )
        ledger.create_account(account_id="p7-executor-account")
        versions = RuntimeDataVersions(
            catalog_version_id="catalog_executor_v3",
            corpus_version_id="corpus_executor_v3",
            index_manifest_id="index_executor_v3",
        )
        with sessions() as session, session.begin():
            session.add(
                V2KnowledgeCorpusVersion(
                    id=versions.corpus_version_id,
                    corpus_name="p7-executor-test",
                    version="v3",
                    status="published",
                    manifest={},
                )
            )
        handler = _SuccessfulHandler()
        durable = DurableReadTurnExecutor(
            sessions,
            handler,
            budget_ledger=ledger,
            allowed_budget_purposes=frozenset({"warmup", "benchmark"}),
        )
        context, case = _context_and_case()
        runtime = cast(
            Any,
            SimpleNamespace(
                policy=SimpleNamespace(
                    variant_id=context.identity.variant_id,
                    rag_enabled=True,
                ),
                shared_services=SimpleNamespace(
                    session_factory=sessions,
                    versions=versions,
                    budget_ledger=ledger,
                    budget_account_id="p7-executor-account",
                ),
                model_calls=_EmptyRecorder(),
                turn_service=V2TurnService(durable, worker_id="p7_executor_test"),
            ),
        )
        executor = EvaluationV3ObservationExecutor(
            runtime=runtime,
            context=context,
            case=case,
        )
        try:
            await executor.reset_initial_state(context=context, case=case)
            request = _turn_request(context, case, case.user_turns[0])
            first = await executor.execute_turn(request)
            attempts_before = ledger.scope_attempt_snapshots(
                cast(str, first.result_payload["turn_id"])
            )
            replay = await executor.execute_turn(request)
            attempts_after = ledger.scope_attempt_snapshots(
                cast(str, replay.result_payload["turn_id"])
            )

            assert handler.calls == 1
            assert first.result_payload["reused"] is False
            assert replay.result_payload["reused"] is True
            assert attempts_after == attempts_before
            assert len(attempts_before) == 1
            assert len(first.model_calls) == 1
            assert first.model_calls[0].input_tokens == 12
            assert first.ledger_events[0].kind.value == "settled_known"
            with sessions() as session:
                scope = session.get(
                    ProviderBudgetScope,
                    cast(str, first.result_payload["turn_id"]),
                )
                assert scope is not None
                assert (
                    scope.purpose,
                    scope.hard_limit_nano_usd,
                    scope.max_generation_calls,
                    scope.max_provider_attempts,
                    scope.max_concurrency,
                ) == ("benchmark", 250_000_000, 10, 16, 2)
        finally:
            engine.dispose()


@pytest.mark.asyncio
async def test_postgres_released_attempt_is_ledger_only_evidence() -> None:
    with _postgres_store("thanh_v2_p2_p7released_") as store:
        context, case = _context_and_case(run_id="run_executor_released")
        handler = _ReleasedHandler()
        executor = EvaluationV3ObservationExecutor(
            runtime=cast(Any, _runtime(store, handler, context)),
            context=context,
            case=case,
        )
        await executor.reset_initial_state(context=context, case=case)
        result = await executor.execute_turn(
            _turn_request(context, case, case.user_turns[0])
        )

        assert handler.calls == 1
        assert result.model_calls == ()
        assert result.embedding_calls == ()
        assert result.retry_events == ()
        assert result.provider_attempts == 0
        assert result.peak_provider_concurrency == 0
        assert result.known_cost_usd == 0
        assert result.unresolved_reserved_cost_usd == 0
        assert len(result.ledger_events) == 1
        assert result.ledger_events[0].kind.value == "released"
        attempts = store.ledger.scope_attempt_snapshots(
            cast(str, result.result_payload["turn_id"])
        )
        assert len(attempts) == 1
        assert attempts[0].transport_started_at is None
        assert attempts[0].actual_cost_nano_usd == 0


@pytest.mark.asyncio
async def test_postgres_cancel_keeps_evidence_without_reexecution() -> None:
    with _postgres_store("thanh_v2_p2_p7cancel_") as store:
        context, case = _context_and_case(run_id="run_executor_cancelled")
        handler = _CancelledHandler()
        executor = EvaluationV3ObservationExecutor(
            runtime=cast(Any, _runtime(store, handler, context)),
            context=context,
            case=case,
        )
        await executor.reset_initial_state(context=context, case=case)
        request = _turn_request(context, case, case.user_turns[0])

        with pytest.raises(ObservationExecutionFailureV3) as first_failure:
            await executor.execute_turn(request)
        assert first_failure.value.safe_error_code == "turn_execution_cancelled"
        first = first_failure.value.partial_results[0]
        assert first.provider_attempts == 1
        assert first.peak_provider_concurrency == 1
        assert first.known_cost_usd == 0
        assert first.unresolved_reserved_cost_usd > 0
        assert first.ledger_events[0].kind.value == "unresolved_reservation"
        scope_id = cast(str, first.result_payload["turn_id"])
        attempts_before = store.ledger.scope_attempt_snapshots(scope_id)

        with pytest.raises(ObservationExecutionFailureV3) as replay_failure:
            await executor.execute_turn(request)
        replay = replay_failure.value.partial_results[0]
        attempts_after = store.ledger.scope_attempt_snapshots(scope_id)

        assert handler.calls == 1
        assert attempts_after == attempts_before
        assert replay.provider_attempts == 1
        assert replay.unresolved_reserved_cost_usd == (
            first.unresolved_reserved_cost_usd
        )


@pytest.mark.asyncio
async def test_postgres_no_rag_enforces_all_concrete_postconditions() -> None:
    with _postgres_store("thanh_v2_p2_p7norag_") as store:

        async def run_case(
            handler: Any,
            *,
            run_id: str,
            recorder: Any | None = None,
        ) -> Any:
            context, case = _context_and_case(
                variant_id="ma_adaptive_no_rag",
                run_id=run_id,
            )
            executor = EvaluationV3ObservationExecutor(
                runtime=cast(
                    Any,
                    _runtime(
                        store,
                        handler,
                        context,
                        rag_enabled=False,
                        recorder=recorder,
                    ),
                ),
                context=context,
                case=case,
            )
            await executor.reset_initial_state(context=context, case=case)
            return await executor.execute_turn(
                _turn_request(context, case, case.user_turns[0])
            )

        zero = await run_case(_SuccessfulHandler(), run_id="run_no_rag_zero")
        assert zero.embedding_calls == ()

        with pytest.raises(
            ObservationExecutionFailureV3,
            match="no_rag_postcondition_failed",
        ) as retrieval_failure:
            await run_case(
                _RetrievalHandler(),
                run_id="run_no_rag_retrieval",
            )
        assert retrieval_failure.value.partial_results

        with pytest.raises(
            ObservationExecutionFailureV3,
            match="no_rag_postcondition_failed",
        ) as embedding_failure:
            await run_case(_EmbeddingHandler(), run_id="run_no_rag_embedding")
        assert embedding_failure.value.partial_results[0].embedding_calls

        recorder = _MutableRecorder()
        with pytest.raises(
            ObservationExecutionFailureV3,
            match="no_rag_postcondition_failed",
        ) as plan_failure:
            await run_case(
                _KnowledgePlanHandler(recorder),
                run_id="run_no_rag_plan",
                recorder=recorder,
            )
        assert plan_failure.value.partial_results


@pytest.mark.asyncio
async def test_postgres_shopping_reset_round_trips_and_rolls_back_collisions() -> None:
    with _postgres_store("thanh_v2_p2_p7shopping_") as store:
        shopper_case = _shopping_case("shopper", "cart")
        shopper_context, _ = _context_and_case(
            shopper_case,
            run_id="run_shopper_fixture",
        )
        shopper = EvaluationV3ObservationExecutor(
            runtime=cast(Any, _runtime(store, _SuccessfulHandler(), shopper_context)),
            context=shopper_context,
            case=shopper_case,
        )
        await shopper.reset_initial_state(
            context=shopper_context,
            case=shopper_case,
        )
        cart_id = shopper.fixture_resource_id("cart", "cart_fixture")
        offer_id = shopper.fixture_resource_id("offer", "1")
        with store.sessions() as session:
            cart = session.get(V2Cart, cart_id)
            lines = tuple(
                session.scalars(select(V2CartLine).where(V2CartLine.cart_id == cart_id))
            )
            offer = session.get(V2Offer, offer_id)
            assert cart is not None and offer is not None
            assert len(lines) == 1
            assert cart.tenant_id == shopper_context.namespace.tenant_id
            assert cart.principal_id == shopper_context.namespace.principal_id
            assert offer.tenant_id == shopper_context.namespace.tenant_id

        merchant_case = _shopping_case("merchant", "merchant")
        merchant_context, _ = _context_and_case(
            merchant_case,
            run_id="run_merchant_fixture",
        )
        merchant = EvaluationV3ObservationExecutor(
            runtime=cast(Any, _runtime(store, _SuccessfulHandler(), merchant_context)),
            context=merchant_context,
            case=merchant_case,
        )
        await merchant.reset_initial_state(
            context=merchant_context,
            case=merchant_case,
        )
        merchant_offer_id = merchant.fixture_resource_id(
            "offer", "merchant_offer_fixture"
        )
        with store.sessions() as session:
            merchant_offer = session.get(V2Offer, merchant_offer_id)
            assert merchant_offer is not None
            assert merchant_offer.tenant_id == merchant_context.namespace.tenant_id
            assert merchant_offer.tenant_id != shopper_context.namespace.tenant_id

        with store.sessions() as session, session.begin():
            session.add(
                V2Order(
                    id="order_fixture_collision",
                    tenant_id=shopper_context.namespace.tenant_id,
                    principal_id=shopper_context.namespace.principal_id,
                    store_id="demo",
                    cart_id=cart_id,
                    cart_version=1,
                    status="confirmed",
                    total_vnd=125_000,
                )
            )
        with pytest.raises(
            ObservationExecutionFailureV3,
            match="evaluation_order_collision",
        ):
            await shopper.reset_initial_state(
                context=shopper_context,
                case=shopper_case,
            )
        with store.sessions() as session:
            assert session.get(V2Order, "order_fixture_collision") is not None
            assert session.get(V2Cart, cart_id) is not None
            assert session.get(V2Offer, offer_id) is not None
            assert tuple(
                session.scalars(select(V2CartLine).where(V2CartLine.cart_id == cart_id))
            )

        foreign_cart_case = _shopping_case("shopper", "cart", suffix="foreign_cart")
        foreign_cart_context, _ = _context_and_case(
            foreign_cart_case,
            run_id="run_foreign_cart_fixture",
        )
        foreign_cart_executor = EvaluationV3ObservationExecutor(
            runtime=cast(
                Any,
                _runtime(store, _SuccessfulHandler(), foreign_cart_context),
            ),
            context=foreign_cart_context,
            case=foreign_cart_case,
        )
        foreign_cart_id = foreign_cart_executor.fixture_resource_id(
            "cart", "cart_foreign_cart"
        )
        with store.sessions() as session, session.begin():
            session.add(
                V2Cart(
                    id=foreign_cart_id,
                    tenant_id="foreign_tenant",
                    principal_id="foreign_principal",
                    store_id="demo",
                    status="active",
                    version=7,
                )
            )
        with pytest.raises(
            ObservationExecutionFailureV3,
            match="evaluation_cart_collision",
        ):
            await foreign_cart_executor.reset_initial_state(
                context=foreign_cart_context,
                case=foreign_cart_case,
            )
        with store.sessions() as session:
            foreign_cart = session.get(V2Cart, foreign_cart_id)
            assert foreign_cart is not None
            assert foreign_cart.tenant_id == "foreign_tenant"
            assert foreign_cart.version == 7

        foreign_offer_case = _shopping_case(
            "merchant", "merchant", suffix="foreign_offer"
        )
        foreign_offer_context, _ = _context_and_case(
            foreign_offer_case,
            run_id="run_foreign_offer_fixture",
        )
        foreign_offer_executor = EvaluationV3ObservationExecutor(
            runtime=cast(
                Any,
                _runtime(store, _SuccessfulHandler(), foreign_offer_context),
            ),
            context=foreign_offer_context,
            case=foreign_offer_case,
        )
        foreign_offer_id = foreign_offer_executor.fixture_resource_id(
            "offer", "merchant_offer_foreign_offer"
        )
        with store.sessions() as session, session.begin():
            session.add(
                V2Offer(
                    id=foreign_offer_id,
                    tenant_id="foreign_tenant",
                    store_id="demo",
                    product_id=2,
                    demo_price_vnd=999_000,
                    stock=9,
                    version=3,
                    is_active=True,
                )
            )
        with pytest.raises(
            ObservationExecutionFailureV3,
            match="evaluation_offer_collision",
        ):
            await foreign_offer_executor.reset_initial_state(
                context=foreign_offer_context,
                case=foreign_offer_case,
            )
        with store.sessions() as session:
            foreign_offer = session.get(V2Offer, foreign_offer_id)
            assert foreign_offer is not None
            assert foreign_offer.tenant_id == "foreign_tenant"
            assert foreign_offer.demo_price_vnd == 999_000


def _shopping_case(
    role: str,
    kind: str,
    *,
    suffix: str = "fixture",
) -> EvaluationCaseV3:
    fixture_id = f"sandbox_{role}_{suffix}"
    if kind == "cart":
        state: dict[str, Any] = {
            "cart": {
                "cart_id": f"cart_{suffix}",
                "version": 1,
                "lines": [
                    {
                        "product_id": 1,
                        "quantity": 1,
                        "unit_price_vnd": 125_000,
                    }
                ],
            },
            "merchant": None,
        }
    else:
        state = {
            "cart": None,
            "merchant": {
                "offers": [
                    {
                        "offer_id": f"merchant_offer_{suffix}",
                        "product_id": 2,
                        "price_vnd": 225_000,
                        "available_quantity": 4,
                        "version": 2,
                    }
                ]
            },
        }
    payload = {
        "fixture_id": fixture_id,
        "reset_revision": 1,
        **state,
    }
    fixture = SandboxFixtureAdapterV3(
        fixture_id=fixture_id,
        reset_revision=1,
        payload=payload,
        fixture_sha256=canonical_sha256(payload),
    )
    return EvaluationCaseV3(
        case_id=f"dev_shopping_{role}_{suffix}",
        work_group_id=f"work_shopping_{role}_{suffix}",
        category="shopping_merchant",
        principal_role=cast(Any, role),
        scopes=(
            ("ecommerce.read", "merchant.read", "merchant.write")
            if role == "merchant"
            else ("ecommerce.read", "shopper.checkout.propose")
        ),
        user_turns=(
            EvaluationUserTurnV3(
                source_turn_id=f"dev_shopping_{role}_{suffix}_t1",
                ordinal=1,
                message="Inspect the isolated shopping fixture",
            ),
        ),
        initial_state={"identity_fixture": {"role": role}},
        sandbox_fixture=fixture,
    )
