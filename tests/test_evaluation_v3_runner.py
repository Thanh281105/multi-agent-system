"""Exactly-once, isolation, attribution, and limit tests for the v3 runner."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_checkpoint import (
    CheckpointIntegrityErrorV3,
    CheckpointWriterV3,
)
from app.evaluation.v3_models import (
    EvaluationAssetBindingsV3,
    EvaluationProtocolV3,
    ObservationIdentityV3,
    ScheduledTurnKindV3,
    ScheduledTurnV3,
    canonical_observation_id_v3,
    canonical_turn_id_v3,
)
from app.evaluation.v3_protocol import (
    build_evaluation_protocol_v3,
    evaluation_protocol_sha256_v3,
    load_evaluation_experiment_v3,
)
from app.evaluation.v3_runner import (
    EmbeddingCallEvidenceV3,
    EvaluationCaseV3,
    EvaluationUserTurnV3,
    InitialStateResetReceiptV3,
    LedgerEventEvidenceV3,
    LedgerEventKindV3,
    ModelCallEvidenceV3,
    ObservationExecutionContextV3,
    ObservationExecutionFailureV3,
    RetryEvidenceV3,
    SandboxFixtureAdapterV3,
    UserTurnExecutionRequestV3,
    UserTurnExecutionResultV3,
    execution_case_set_sha256_v3,
    execution_case_sha256_v3,
    initial_state_reset_receipt_v3,
    run_observations_v3,
)
from app.evaluation.v3_schedule import (
    SCHEDULE_ALGORITHM_ID_V3,
    pilot_schedule_sha256_v3,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_PATH = PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"


def _protocol() -> EvaluationProtocolV3:
    assets = EvaluationAssetBindingsV3.model_validate(
        {
            field_name: f"{index + 1:064x}"
            for index, field_name in enumerate(EvaluationAssetBindingsV3.model_fields)
        }
    )
    return build_evaluation_protocol_v3(
        load_evaluation_experiment_v3(EXPERIMENT_PATH),
        assets=assets,
    )


def _schedule(
    protocol: EvaluationProtocolV3,
    specifications: Sequence[tuple[ScheduledTurnKindV3, str, str, int]],
    *,
    run_id: str = "run_runner_v3",
) -> tuple[ScheduledTurnV3, ...]:
    protocol_sha = evaluation_protocol_sha256_v3(protocol)
    case_groups = {
        case.case_id: case.work_group_id for case in protocol.experiment.pilot_cases
    }
    turns: list[ScheduledTurnV3] = []
    execution_order = 0
    for schedule_index, (kind, variant_id, case_id, repetition) in enumerate(
        specifications
    ):
        identity = ObservationIdentityV3(
            run_id=run_id,
            protocol_sha256=protocol_sha,
            schedule_algorithm_id=SCHEDULE_ALGORITHM_ID_V3,
            turn_kind=kind,
            variant_id=variant_id,
            case_id=case_id,
            work_group_id=case_groups[case_id],
            repetition=repetition,
        )
        is_measured = kind is ScheduledTurnKindV3.MEASURED
        turns.append(
            ScheduledTurnV3(
                turn_id=canonical_turn_id_v3(identity),
                observation_id=(
                    canonical_observation_id_v3(identity) if is_measured else None
                ),
                identity=identity,
                schedule_index=schedule_index,
                execution_order=execution_order if is_measured else None,
            )
        )
        if is_measured:
            execution_order += 1
    return tuple(turns)


def _cases(
    protocol: EvaluationProtocolV3,
    schedule: Sequence[ScheduledTurnV3],
    *,
    multi_turn_case_id: str | None = None,
) -> dict[str, EvaluationCaseV3]:
    groups = {
        case.case_id: case.work_group_id for case in protocol.experiment.pilot_cases
    }
    return {
        case_id: EvaluationCaseV3(
            case_id=case_id,
            work_group_id=groups[case_id],
            category=(
                "multi_turn_memory"
                if case_id == multi_turn_case_id
                else "multi_constraint"
            ),
            principal_role="shopper",
            scopes=("ecommerce.read",),
            user_turns=tuple(
                EvaluationUserTurnV3(
                    source_turn_id=f"{case_id}_t{ordinal}",
                    ordinal=ordinal,
                    message=f"message {ordinal}",
                )
                for ordinal in range(
                    1,
                    3 if case_id == multi_turn_case_id else 2,
                )
            ),
            initial_state={"cart": [], "memory": {}},
        )
        for case_id in {turn.identity.case_id for turn in schedule}
    }


class _RecordingFactory:
    def __init__(
        self,
        *,
        fail_turn_ids: frozenset[str] = frozenset(),
        attempts: int = 1,
        peak_concurrency: int = 1,
        generation_calls_per_turn: int = 1,
        known_cost_per_turn: Decimal = Decimal("0.001"),
    ) -> None:
        self.fail_turn_ids = fail_turn_ids
        self.attempts = attempts
        self.peak_concurrency = peak_concurrency
        self.generation_calls_per_turn = generation_calls_per_turn
        self.known_cost_per_turn = known_cost_per_turn
        self.contexts: list[ObservationExecutionContextV3] = []
        self.resets: list[tuple[ObservationExecutionContextV3, dict[str, object]]] = []
        self.reset_cases: list[EvaluationCaseV3] = []
        self.requests: list[UserTurnExecutionRequestV3] = []
        self.executor_requests: list[list[UserTurnExecutionRequestV3]] = []

    def __call__(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> _RecordingExecutor:
        del case
        self.contexts.append(context)
        local_requests: list[UserTurnExecutionRequestV3] = []
        self.executor_requests.append(local_requests)
        return _RecordingExecutor(self, context, local_requests)


class _RecordingExecutor:
    def __init__(
        self,
        factory: _RecordingFactory,
        context: ObservationExecutionContextV3,
        local_requests: list[UserTurnExecutionRequestV3],
    ) -> None:
        self.factory = factory
        self.context = context
        self.local_requests = local_requests

    async def reset_initial_state(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> InitialStateResetReceiptV3:
        assert context is self.context
        self.factory.resets.append((context, dict(case.initial_state)))
        self.factory.reset_cases.append(case)
        if context.canonical_turn_id in self.factory.fail_turn_ids:
            raise ObservationExecutionFailureV3("synthetic_observation_failure")
        return initial_state_reset_receipt_v3(context, case)

    async def execute_turn(
        self,
        request: UserTurnExecutionRequestV3,
    ) -> UserTurnExecutionResultV3:
        assert request.context is self.context
        self.factory.requests.append(request)
        self.local_requests.append(request)
        attribution = request.attribution
        digest = canonical_sha256(
            {
                "execution_turn_id": attribution.execution_turn_id,
                "source_turn_id": attribution.source_turn_id,
            }
        )
        embedding_call_id = f"ecall_{digest}"
        model_calls: list[ModelCallEvidenceV3] = []
        retry_events: list[RetryEvidenceV3] = []
        for call_index in range(self.factory.generation_calls_per_turn):
            call_digest = canonical_sha256(
                {"evidence_digest": digest, "generation_call_index": call_index}
            )
            model_call_id = f"mcall_{call_digest}"
            model_calls.append(
                ModelCallEvidenceV3(
                    call_id=model_call_id,
                    attribution=attribution,
                    model="gpt-5.4-mini-2026-03-17",
                    attempts=self.factory.attempts,
                    input_tokens=10,
                    cached_input_tokens=2,
                    output_tokens=5,
                    reasoning_tokens=1,
                    total_tokens=15,
                )
            )
            if self.factory.attempts == 2:
                retry_events.append(
                    RetryEvidenceV3(
                        retry_event_id=f"retry_{call_digest}",
                        attribution=attribution,
                        call_id=model_call_id,
                        retry_ordinal=1,
                    )
                )
        return UserTurnExecutionResultV3(
            attribution=attribution,
            result_payload={"answer": "bounded", "completed": True},
            model_calls=tuple(model_calls),
            embedding_calls=(
                EmbeddingCallEvidenceV3(
                    call_id=embedding_call_id,
                    attribution=attribution,
                    model="text-embedding-3-small",
                    attempts=1,
                    input_tokens=3,
                ),
            ),
            retry_events=tuple(retry_events),
            ledger_events=(
                LedgerEventEvidenceV3(
                    ledger_event_id=f"ledger_{digest}",
                    attribution=attribution,
                    kind=LedgerEventKindV3.SETTLED_KNOWN,
                    known_cost_usd=self.factory.known_cost_per_turn,
                ),
            ),
            peak_provider_concurrency=self.factory.peak_concurrency,
            max_attempt_duration_seconds=0.1,
            elapsed_seconds=0.2,
        )


class _ForbiddenFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_: object) -> _RecordingExecutor:
        self.calls += 1
        raise AssertionError("a terminal or ambiguous observation was re-executed")


class _UnacknowledgedResetFactory:
    def __init__(self) -> None:
        self.reset_cases: list[EvaluationCaseV3] = []
        self.execute_calls = 0

    def __call__(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> _UnacknowledgedResetExecutor:
        del context, case
        return _UnacknowledgedResetExecutor(self)


class _UnacknowledgedResetExecutor:
    def __init__(self, factory: _UnacknowledgedResetFactory) -> None:
        self.factory = factory

    async def reset_initial_state(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> None:
        del context
        self.factory.reset_cases.append(case)

    async def execute_turn(
        self,
        request: UserTurnExecutionRequestV3,
    ) -> UserTurnExecutionResultV3:
        del request
        self.factory.execute_calls += 1
        raise AssertionError("provider work ran without an acknowledged sandbox reset")


@pytest.mark.asyncio
async def test_namespaces_isolate_variants_cases_and_repeats_but_keep_turns_continuous(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    case_ids = tuple(case.case_id for case in protocol.experiment.pilot_cases[:2])
    variant_ids = tuple(
        variant.variant_id for variant in protocol.experiment.variants[:2]
    )
    specifications = tuple(
        (ScheduledTurnKindV3.MEASURED, variant_id, case_id, repetition)
        for case_id in case_ids
        for repetition in (0, 1)
        for variant_id in variant_ids
    )
    schedule = _schedule(protocol, specifications)
    cases = _cases(
        protocol,
        schedule,
        multi_turn_case_id=case_ids[0],
    )
    factory = _RecordingFactory()

    result = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=tmp_path / "checkpoint.jsonl",
        cases=cases,
        executor_factory=factory,
    )

    assert len(factory.contexts) == len(schedule) == 8
    assert len({context.namespace for context in factory.contexts}) == len(schedule)
    assert len({context.namespace.tenant_id for context in factory.contexts}) == 8
    assert len({context.namespace.principal_id for context in factory.contexts}) == 8
    assert len({context.namespace.conversation_id for context in factory.contexts}) == 8
    assert all(initial == {"cart": [], "memory": {}} for _, initial in factory.resets)
    for context, requests in zip(
        factory.contexts,
        factory.executor_requests,
        strict=True,
    ):
        assert requests
        assert {request.context.namespace for request in requests} == {
            context.namespace
        }
        assert len({request.trace_id for request in requests}) == 1
        execution_turn_ids = {
            request.attribution.execution_turn_id for request in requests
        }
        assert len(execution_turn_ids) == len(requests)
    assert result.summary.completed_turn_ids == tuple(turn.turn_id for turn in schedule)
    assert result.summary.missing_turn_ids == ()
    assert result.summary.is_partial is False


@pytest.mark.asyncio
async def test_multi_turn_limits_and_deadlines_are_applied_per_user_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _protocol()
    case_id = protocol.experiment.pilot_cases[0].case_id
    schedule = _schedule(
        protocol,
        (
            (
                ScheduledTurnKindV3.MEASURED,
                "ma_adaptive_rag",
                case_id,
                0,
            ),
        ),
        run_id="run_per_user_turn_limits_v3",
    )
    cases = _cases(protocol, schedule, multi_turn_case_id=case_id)
    timeout_entries: list[float] = []

    class _TimeoutProbe:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object,
        ) -> None:
            del exc_type, exc, traceback

    def timeout_probe(seconds: float) -> _TimeoutProbe:
        timeout_entries.append(seconds)
        return _TimeoutProbe()

    monkeypatch.setattr("app.evaluation.v3_runner.asyncio.timeout", timeout_probe)
    result = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=tmp_path / "checkpoint.jsonl",
        cases=cases,
        executor_factory=_RecordingFactory(
            generation_calls_per_turn=6,
            known_cost_per_turn=Decimal("0.20"),
        ),
    )

    receipt = result.receipts[0]
    assert result.summary.completed_turn_ids == (schedule[0].turn_id,)
    assert len(receipt.turn_results) == 2
    assert receipt.totals.generation_calls == 12
    assert receipt.totals.provider_attempts == 14
    assert receipt.totals.known_cost_usd == Decimal("0.40")
    assert all(turn.generation_calls == 6 for turn in receipt.turn_results)
    assert all(turn.known_cost_usd == Decimal("0.20") for turn in receipt.turn_results)
    assert timeout_entries == [60.0, 60.0, 60.0]


@pytest.mark.asyncio
async def test_crash_after_started_blocks_resume_without_duplicate_execution(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    case_ids = tuple(case.case_id for case in protocol.experiment.pilot_cases[:2])
    schedule = _schedule(
        protocol,
        (
            (
                ScheduledTurnKindV3.MEASURED,
                "sa_shared_tools_rag",
                case_ids[0],
                0,
            ),
            (ScheduledTurnKindV3.MEASURED, "ma_fixed_rag", case_ids[1], 0),
        ),
        run_id="run_crash_resume_v3",
    )
    path = tmp_path / "checkpoint.jsonl"
    protocol_sha = evaluation_protocol_sha256_v3(protocol)
    schedule_sha = pilot_schedule_sha256_v3(schedule)
    cases = _cases(protocol, schedule)
    with CheckpointWriterV3(
        path,
        run_id="run_crash_resume_v3",
        protocol_sha256=protocol_sha,
        execution_case_set_sha256=execution_case_set_sha256_v3(schedule, cases),
        schedule_sha256=schedule_sha,
        schedule=schedule,
    ) as writer:
        writer.append_started(schedule[0])

    resumed_factory = _RecordingFactory()
    result = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=path,
        cases=cases,
        executor_factory=resumed_factory,
    )

    assert [context.canonical_turn_id for context in resumed_factory.contexts] == [
        schedule[1].turn_id
    ]
    assert result.summary.blocked_on_ambiguous_work is True
    assert result.summary.ambiguous_turn_ids == (schedule[0].turn_id,)
    assert result.summary.orphan_started_turn_ids == ()
    assert result.summary.completed_turn_ids == (schedule[1].turn_id,)
    assert result.summary.pending_turn_ids == ()
    assert result.summary.missing_turn_ids == (schedule[0].turn_id,)
    assert result.summary.is_partial is True
    ambiguity_payload = next(
        document["payload"]["ambiguity"]
        for document in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if document["event"] == "ambiguous"
    )
    orphan_case = cases[schedule[0].identity.case_id]
    assert ambiguity_payload["execution_case_sha256"] == execution_case_sha256_v3(
        orphan_case
    )
    assert (
        ambiguity_payload["execution_case_set_sha256"]
        == result.summary.execution_case_set_sha256
    )

    before = path.read_bytes()
    forbidden = _ForbiddenFactory()
    restarted = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=path,
        cases=cases,
        executor_factory=forbidden,
    )
    assert forbidden.calls == 0
    assert restarted == result
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_shopping_fixture_requires_explicit_bound_reset_acknowledgment(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    case_binding = protocol.experiment.pilot_cases[-1]
    fixture_payload = {
        "fixture_id": f"sandbox_{case_binding.case_id}",
        "reset_revision": 1,
        "cart": {
            "cart_id": "cart_runner_v3",
            "version": 1,
            "lines": [{"product_id": 1, "quantity": 1, "unit_price_vnd": 10}],
        },
        "merchant": None,
        "target_capability_id": "shopper.checkout.propose",
        "proposal_parameters": {
            "kind": "checkout_proposal",
            "capability_id": "shopper.checkout.propose",
            "cart_id": "cart_runner_v3",
            "expected_version": 1,
        },
        "confirmed_proposal_id": None,
    }
    fixture = SandboxFixtureAdapterV3(
        fixture_id=str(fixture_payload["fixture_id"]),
        fixture_sha256=canonical_sha256(fixture_payload),
        reset_revision=1,
        payload=fixture_payload,
    )
    case = EvaluationCaseV3(
        case_id=case_binding.case_id,
        work_group_id=case_binding.work_group_id,
        category="shopping_merchant",
        principal_role="shopper",
        scopes=("ecommerce.read", "ecommerce.write"),
        user_turns=(
            EvaluationUserTurnV3(
                source_turn_id=f"{case_binding.case_id}_t1",
                ordinal=1,
                message="prepare checkout",
            ),
        ),
        initial_state={"confirmation": "unconfirmed"},
        sandbox_fixture=fixture,
    )
    specifications = (
        (
            ScheduledTurnKindV3.MEASURED,
            "ma_adaptive_rag",
            case_binding.case_id,
            0,
        ),
    )
    unacknowledged_schedule = _schedule(
        protocol,
        specifications,
        run_id="run_shopping_unacknowledged_v3",
    )
    unacknowledged = _UnacknowledgedResetFactory()
    failed = await run_observations_v3(
        protocol=protocol,
        schedule=unacknowledged_schedule,
        checkpoint_path=tmp_path / "unacknowledged.jsonl",
        cases={case.case_id: case},
        executor_factory=unacknowledged,
    )
    assert unacknowledged.execute_calls == 0
    assert unacknowledged.reset_cases == [case]
    assert failed.summary.failed_turn_ids == (unacknowledged_schedule[0].turn_id,)
    assert failed.receipts[0].safe_error_code == "initial_state_reset_unacknowledged"

    acknowledged_schedule = _schedule(
        protocol,
        specifications,
        run_id="run_shopping_acknowledged_v3",
    )
    acknowledged = _RecordingFactory()
    completed = await run_observations_v3(
        protocol=protocol,
        schedule=acknowledged_schedule,
        checkpoint_path=tmp_path / "acknowledged.jsonl",
        cases={case.case_id: case},
        executor_factory=acknowledged,
    )
    reset = completed.receipts[0].initial_state_reset_receipt
    assert acknowledged.reset_cases == [case]
    assert reset is not None
    assert reset.sandbox_fixture_id == fixture.fixture_id
    assert reset.sandbox_fixture_sha256 == fixture.fixture_sha256
    assert completed.summary.completed_turn_ids == (acknowledged_schedule[0].turn_id,)


@pytest.mark.asyncio
async def test_completed_and_failed_terminal_records_skip_on_deterministic_restart(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    case_ids = tuple(case.case_id for case in protocol.experiment.pilot_cases[:2])
    schedule = _schedule(
        protocol,
        (
            (
                ScheduledTurnKindV3.MEASURED,
                "sa_shared_tools_rag",
                case_ids[0],
                0,
            ),
            (ScheduledTurnKindV3.MEASURED, "ma_fixed_rag", case_ids[1], 0),
        ),
        run_id="run_terminal_restart_v3",
    )
    path = tmp_path / "checkpoint.jsonl"
    cases = _cases(protocol, schedule)
    first_factory = _RecordingFactory(fail_turn_ids=frozenset({schedule[1].turn_id}))
    first = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=path,
        cases=cases,
        executor_factory=first_factory,
    )
    before = path.read_bytes()

    forbidden = _ForbiddenFactory()
    second = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=path,
        cases=cases,
        executor_factory=forbidden,
    )

    assert forbidden.calls == 0
    assert second == first
    assert path.read_bytes() == before
    assert first.summary.completed_turn_ids == (schedule[0].turn_id,)
    assert first.summary.failed_turn_ids == (schedule[1].turn_id,)
    assert first.summary.pending_turn_ids == ()
    assert first.summary.missing_turn_ids == (schedule[1].turn_id,)


@pytest.mark.asyncio
async def test_resume_rejects_changed_case_content_before_executor_runs(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    case_id = protocol.experiment.pilot_cases[0].case_id
    schedule = _schedule(
        protocol,
        (
            (
                ScheduledTurnKindV3.MEASURED,
                "ma_adaptive_rag",
                case_id,
                0,
            ),
        ),
        run_id="run_stale_case_resume_v3",
    )
    cases = _cases(protocol, schedule)
    path = tmp_path / "checkpoint.jsonl"
    await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=path,
        cases=cases,
        executor_factory=_RecordingFactory(),
    )

    original = cases[case_id]
    changed_turns = [turn.model_dump(mode="json") for turn in original.user_turns]
    changed_turns[0]["message"] = "a changed message with the same IDs"
    changed = EvaluationCaseV3.model_validate(
        {
            **original.model_dump(mode="json"),
            "user_turns": changed_turns,
        }
    )
    changed_cases = {case_id: changed}
    assert execution_case_sha256_v3(changed) != execution_case_sha256_v3(original)
    assert execution_case_set_sha256_v3(
        schedule,
        changed_cases,
    ) != execution_case_set_sha256_v3(schedule, cases)

    forbidden = _ForbiddenFactory()
    with pytest.raises(
        CheckpointIntegrityErrorV3,
        match="foreign execution case set",
    ):
        await run_observations_v3(
            protocol=protocol,
            schedule=schedule,
            checkpoint_path=path,
            cases=changed_cases,
            executor_factory=forbidden,
        )
    assert forbidden.calls == 0


@pytest.mark.asyncio
async def test_warmup_receipt_and_all_provider_evidence_use_canonical_attribution(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    warmup_case = protocol.experiment.warmup_case_id
    measured_case = protocol.experiment.pilot_cases[1].case_id
    schedule = _schedule(
        protocol,
        (
            (
                ScheduledTurnKindV3.WARMUP,
                "sa_shared_tools_rag",
                warmup_case,
                0,
            ),
            (
                ScheduledTurnKindV3.MEASURED,
                "ma_adaptive_rag",
                measured_case,
                0,
            ),
        ),
        run_id="run_attribution_v3",
    )
    factory = _RecordingFactory(attempts=2)
    cases = _cases(protocol, schedule)
    result = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=tmp_path / "checkpoint.jsonl",
        cases=cases,
        executor_factory=factory,
    )

    assert len(result.pilot_costs) == 2
    assert len(result.pilot_cost_evidence) == 2
    assert result.pilot_costs[0].is_warmup is True
    assert result.pilot_costs[1].is_warmup is False
    assert result.pilot_costs[0].known_cost_usd == Decimal("0.001")
    for receipt, scheduled in zip(result.receipts, schedule, strict=True):
        case = cases[scheduled.identity.case_id]
        assert receipt.canonical_turn_id == scheduled.turn_id
        assert receipt.observation_id == scheduled.observation_id
        assert receipt.execution_case_sha256 == execution_case_sha256_v3(case)
        assert (
            receipt.execution_case_set_sha256
            == result.summary.execution_case_set_sha256
        )
        assert receipt.initial_state_reset is True
        assert receipt.initial_state_reset_receipt is not None
        assert (
            receipt.initial_state_reset_receipt.execution_case_sha256
            == receipt.execution_case_sha256
        )
        assert receipt.totals.retry_count == 1
        for turn_result in receipt.turn_results:
            evidence = (
                *turn_result.model_calls,
                *turn_result.embedding_calls,
                *turn_result.retry_events,
                *turn_result.ledger_events,
            )
            assert evidence
            assert all(
                item.attribution.canonical_turn_id == scheduled.turn_id
                and item.attribution.observation_id == scheduled.observation_id
                for item in evidence
            )
            assert turn_result.ledger_events[0].ledger_event_id in (
                receipt.pilot_cost.ledger_event_ids
                if receipt.pilot_cost is not None
                else ()
            )


@pytest.mark.asyncio
async def test_resource_limit_violation_is_terminal_and_never_retried(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    case_id = protocol.experiment.pilot_cases[0].case_id
    schedule = _schedule(
        protocol,
        (
            (
                ScheduledTurnKindV3.MEASURED,
                "sa_shared_tools_rag",
                case_id,
                0,
            ),
        ),
        run_id="run_limit_violation_v3",
    )
    path = tmp_path / "checkpoint.jsonl"
    cases = _cases(protocol, schedule)
    first = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=path,
        cases=cases,
        executor_factory=_RecordingFactory(peak_concurrency=3),
    )
    assert first.summary.failed_turn_ids == (schedule[0].turn_id,)
    assert first.receipts[0].safe_error_code == "provider_concurrency_limit_exceeded"
    assert first.receipts[0].limits.provider_concurrency == 2
    assert first.receipts[0].limits.max_generation_calls == 10
    assert first.receipts[0].limits.max_provider_attempts == 16
    assert first.receipts[0].limits.max_retries == 1
    assert first.receipts[0].limits.attempt_timeout_seconds == 18.0
    assert first.receipts[0].limits.turn_deadline_seconds == 60.0
    assert first.receipts[0].limits.per_turn_limit_usd == Decimal("0.25")

    forbidden = _ForbiddenFactory()
    await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        checkpoint_path=path,
        cases=cases,
        executor_factory=forbidden,
    )
    assert forbidden.calls == 0
