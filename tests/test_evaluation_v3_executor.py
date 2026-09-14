"""Focused unit tests for the concrete Package 7 observation executor."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.evaluation.v3_executor import (
    EvaluationV3ObservationExecutor,
    _turn_result,
    build_evaluation_cases_v3,
)
from app.evaluation.v3_gold import load_evaluation_gold_v3
from app.evaluation.v3_models import (
    EvaluationAssetBindingsV3,
    ObservationIdentityV3,
    ScheduledTurnKindV3,
    canonical_observation_id_v3,
    canonical_turn_id_v3,
)
from app.evaluation.v3_protocol import (
    build_evaluation_protocol_v3,
    load_evaluation_experiment_v3,
)
from app.evaluation.v3_runner import (
    EvaluationCaseV3,
    EvaluationResourceLimitsV3,
    EvaluationUserTurnV3,
    ObservationExecutionContextV3,
    ObservationExecutionFailureV3,
    _turn_request,
    execution_case_sha256_v3,
    execution_namespace_v3,
)
from app.evaluation.v3_schedule import SCHEDULE_ALGORITHM_ID_V3
from app.models.v2 import V2Conversation
from app.shared.budget import (
    PricingManifest,
    SQLProviderBudgetLedger,
    default_pricing_manifest_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _loaded_and_protocol() -> tuple[Any, Any]:
    loaded = load_evaluation_gold_v3(
        PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json",
        PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json",
        project_root=PROJECT_ROOT,
    )
    fields = {
        name: f"{index + 1:064x}"
        for index, name in enumerate(EvaluationAssetBindingsV3.model_fields)
    }
    fields["gold_sha256"] = loaded.gold_sha256
    fields["split_sha256"] = loaded.split_sha256
    protocol = build_evaluation_protocol_v3(
        load_evaluation_experiment_v3(
            PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"
        ),
        assets=EvaluationAssetBindingsV3.model_validate(fields),
    )
    return loaded, protocol


def test_build_cases_binds_exact_loaded_pilot_roster() -> None:
    loaded, protocol = _loaded_and_protocol()
    cases = build_evaluation_cases_v3(loaded, protocol)

    assert tuple(cases) == loaded.gold.frozen_pilot_ids
    assert tuple(cases) == tuple(item.case_id for item in protocol.pilot_cases)
    for binding in protocol.pilot_cases:
        case = cases[binding.case_id]
        assert case.work_group_id == binding.work_group_id
        assert execution_case_sha256_v3(case)
    shopping = cases["dev_shopping_merchant_01"]
    assert shopping.sandbox_fixture is not None
    assert shopping.sandbox_fixture.fixture_id == ("sandbox_dev_shopping_merchant_01")


def test_executor_reset_uses_compact_owned_conversation_and_rejects_collision(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'executor.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    context, case = _context_and_case()
    runtime = cast(
        Any,
        SimpleNamespace(
            policy=SimpleNamespace(variant_id=context.identity.variant_id),
            shared_services=SimpleNamespace(session_factory=sessions),
        ),
    )
    executor = EvaluationV3ObservationExecutor(
        runtime=runtime,
        context=context,
        case=case,
    )
    try:
        receipt = __import__("asyncio").run(
            executor.reset_initial_state(context=context, case=case)
        )
        assert receipt.reset_completed
        assert len(executor.conversation_id) <= 64
        with sessions() as session:
            conversation = session.scalar(
                select(V2Conversation).where(
                    V2Conversation.id == executor.conversation_id
                )
            )
            assert conversation is not None
            assert conversation.tenant_id == context.namespace.tenant_id

        with sessions() as session, session.begin():
            conversation = session.get(V2Conversation, executor.conversation_id)
            assert conversation is not None
            conversation.tenant_id = "foreign_tenant"
        with pytest.raises(
            ObservationExecutionFailureV3,
            match="evaluation_conversation_collision",
        ):
            __import__("asyncio").run(
                executor.reset_initial_state(context=context, case=case)
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_released_reservation_remains_ledger_only_evidence(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'released.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    ledger = SQLProviderBudgetLedger(
        sessions,
        PricingManifest.load(default_pricing_manifest_path()),
    )
    ledger.create_account(account_id="released-unit")
    ledger.create_scope(
        scope_id="turn_released_unit",
        account_id="released-unit",
        purpose="benchmark",
    )
    reservation = ledger.reserve_attempt(
        scope_id="turn_released_unit",
        call_id="mcall_released_unit",
        attempt_number=1,
        operation="generation",
        purpose="benchmark",
        model="gpt-5.4-mini-2026-03-17",
        request_fingerprint_sha256="e" * 64,
        input_token_bound=20,
        output_token_bound=10,
    )
    ledger.abandon_undispatched(
        scope_id="turn_released_unit",
        attempt_id=reservation.attempt_id,
        error_code="provider_dispatch_cancelled",
    )
    context, case = _context_and_case()
    request = _turn_request(context, case, case.user_turns[0])
    try:
        result = _turn_result(
            request=request,
            outcome_payload={"status": "failed"},
            attempts=ledger.scope_attempt_snapshots("turn_released_unit"),
            elapsed_seconds=0.01,
        )
        assert result.model_calls == ()
        assert result.embedding_calls == ()
        assert result.retry_events == ()
        assert result.provider_attempts == 0
        assert result.peak_provider_concurrency == 0
        assert result.known_cost_usd == 0
        assert result.unresolved_reserved_cost_usd == 0
        assert len(result.ledger_events) == 1
        assert result.ledger_events[0].kind.value == "released"
        assert result.ledger_events[0].reservation_id == reservation.attempt_id
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _context_and_case(
    case: EvaluationCaseV3 | None = None,
    *,
    variant_id: str = "ma_adaptive_rag",
    run_id: str = "run_executor_unit",
) -> tuple[ObservationExecutionContextV3, EvaluationCaseV3]:
    case = case or EvaluationCaseV3(
        case_id="dev_multi_constraint_01",
        work_group_id="work_unit_executor",
        category="multi_constraint",
        principal_role="shopper",
        scopes=("ecommerce.read",),
        user_turns=(
            EvaluationUserTurnV3(
                source_turn_id="dev_multi_constraint_01_t1",
                ordinal=1,
                message="Find one book",
            ),
        ),
        initial_state={"identity_fixture": {"role": "shopper"}},
    )
    identity = ObservationIdentityV3(
        run_id=run_id,
        protocol_sha256="a" * 64,
        schedule_algorithm_id=SCHEDULE_ALGORITHM_ID_V3,
        turn_kind=ScheduledTurnKindV3.MEASURED,
        variant_id=variant_id,  # type: ignore[arg-type]
        case_id=case.case_id,
        work_group_id=case.work_group_id,
        repetition=0,
    )
    turn_id = canonical_turn_id_v3(identity)
    observation_id = canonical_observation_id_v3(identity)
    case_sha = execution_case_sha256_v3(case)
    namespace = execution_namespace_v3(
        run_id=identity.run_id,
        protocol_sha256=identity.protocol_sha256,
        execution_case_sha256=case_sha,
        execution_case_set_sha256="b" * 64,
        schedule_sha256="c" * 64,
        canonical_turn_id=turn_id,
        observation_id=observation_id,
    )
    return (
        ObservationExecutionContextV3(
            run_id=identity.run_id,
            protocol_sha256=identity.protocol_sha256,
            execution_case_sha256=case_sha,
            execution_case_set_sha256="b" * 64,
            schedule_sha256="c" * 64,
            schedule_index=0,
            canonical_turn_id=turn_id,
            observation_id=observation_id,
            identity=identity,
            namespace=namespace,
            limits=EvaluationResourceLimitsV3(
                provider_concurrency=2,
                max_generation_calls=10,
                max_provider_attempts=16,
                max_retries=1,
                attempt_timeout_seconds=18.0,
                turn_deadline_seconds=60.0,
                per_turn_limit_usd="0.25",
                max_input_tokens_per_generation=12_000,
                max_output_tokens_per_generation=1_200,
            ),
        ),
        case,
    )
