"""Focused contract tests for the additive Package 8 held-out driver."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.evaluation import benchmark_cli, benchmark_v3
from app.evaluation.benchmark_v3 import (
    EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3,
    FrozenPackage7DriftError,
    HeldoutEvaluationV3ObservationRunner,
    build_heldout_execution_plan_v3,
    build_heldout_schedule_v3,
    load_frozen_package7_heldout_inputs_v3,
    package8_additive_source_sha256_v3,
    run_heldout_benchmark_v3,
    validate_heldout_execution_plan_v3,
)
from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_checkpoint import CheckpointWriterV3
from app.evaluation.v3_gold import EvaluationSplitV3
from app.evaluation.v3_models import (
    PACKAGE7_VARIANT_ORDER,
    EvaluationProtocolV3,
    RepeatDecisionV3,
    VariantCostProjectionV3,
)
from app.evaluation.v3_protocol import evaluation_protocol_sha256_v3
from app.evaluation.v3_runner import (
    EvaluationCaseV3,
    InitialStateResetReceiptV3,
    LedgerEventEvidenceV3,
    LedgerEventKindV3,
    ModelCallEvidenceV3,
    ObservationExecutionContextV3,
    ObservationExecutorV3,
    UserTurnExecutionRequestV3,
    UserTurnExecutionResultV3,
    initial_state_reset_receipt_v3,
)
from app.evaluation.v3_schedule import pilot_schedule_sha256_v3

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def frozen(tmp_path_factory: pytest.TempPathFactory):
    from app.evaluation import v3_cli

    artifact_dir = tmp_path_factory.mktemp("package7-fixture")
    arguments = v3_cli._parser().parse_args(
        ("validate", "--project-root", str(PROJECT_ROOT))
    )
    p7_inputs = v3_cli._load_inputs(arguments)
    protocol = p7_inputs.protocol
    protocol_path, decision_path = _write_package7_artifacts(
        artifact_dir,
        protocol,
        RepeatDecisionV3(
            protocol_sha256=evaluation_protocol_sha256_v3(protocol),
            repeat_rule_sha256=protocol.repeat_rule_sha256,
            cost_evidence_sha256="0" * 64,
            selected_repeats=3,
            pilot_effective_cost_usd=Decimal("0"),
            projected_benchmark_cost_usd=Decimal("0"),
            per_variant=tuple(
                VariantCostProjectionV3(
                    variant_id=variant_id,
                    maximum_pilot_observation_cost_usd=Decimal("0"),
                )
                for variant_id in PACKAGE7_VARIANT_ORDER
            ),
        ),
    )
    return load_frozen_package7_heldout_inputs_v3(
        project_root=PROJECT_ROOT,
        protocol_path=protocol_path,
        repeat_decision_path=decision_path,
        gold_path=PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json",
        split_path=PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json",
    )


def test_p8_preserves_the_frozen_package7_source_manifest_and_protocol(frozen) -> None:
    digest = hashlib.sha256()
    for path in sorted(
        (PROJECT_ROOT / "app" / "evaluation").glob("v3_*.py"),
        key=lambda item: item.as_posix(),
    ):
        content_sha256 = hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        digest.update(
            f"{path.relative_to(PROJECT_ROOT).as_posix()}\0{content_sha256}\n".encode(
                "utf-8"
            )
        )

    assert frozen.protocol_sha256 == EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3
    assert frozen.protocol.assets.evaluator_sha256 == digest.hexdigest()
    plan_schedule = build_heldout_schedule_v3(frozen, run_id="run_p8_manifest")
    plan = build_heldout_execution_plan_v3(
        frozen,
        plan_schedule,
        run_id="run_p8_manifest",
    )
    manifest_digest = hashlib.sha256()
    for name in ("benchmark_cli.py", "benchmark_v3.py"):
        path = PROJECT_ROOT / "app" / "evaluation" / name
        content_sha256 = hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        manifest_digest.update(
            f"app/evaluation/{name}\0{content_sha256}\n".encode("utf-8")
        )
    assert plan.additive_source_manifest_sha256 == manifest_digest.hexdigest()
    assert plan.additive_source_manifest_sha256 == package8_additive_source_sha256_v3()
    assert plan.additive_source_files == (
        "app/evaluation/benchmark_cli.py",
        "app/evaluation/benchmark_v3.py",
    )


def test_p8_schedule_is_a_deterministic_complete_720_cell_matrix(frozen) -> None:
    first = build_heldout_schedule_v3(frozen, run_id="run_p8_schedule")
    second = build_heldout_schedule_v3(frozen, run_id="run_p8_schedule")
    first_plan = build_heldout_execution_plan_v3(
        frozen,
        first,
        run_id="run_p8_schedule",
    )
    second_plan = build_heldout_execution_plan_v3(
        frozen,
        second,
        run_id="run_p8_schedule",
    )

    assert first == second
    assert pilot_schedule_sha256_v3(first) == pilot_schedule_sha256_v3(second)
    assert first_plan == second_plan
    assert first_plan.selected_repeats == 3
    assert first_plan.measured_cell_count == 720
    assert len(first) == 720
    assert all(turn.identity.turn_kind.value == "measured" for turn in first)
    assert all(turn.observation_id is not None for turn in first)
    assert [turn.schedule_index for turn in first] == list(range(720))
    assert [turn.execution_order for turn in first] == list(range(720))
    assert {
        (turn.identity.case_id, turn.identity.variant_id, turn.identity.repetition)
        for turn in first
    } == {
        (case_id, variant_id, repetition)
        for case_id in frozen.heldout_cases
        for variant_id in (
            "sa_shared_tools_rag",
            "ma_fixed_rag",
            "ma_adaptive_rag",
            "ma_adaptive_no_rag",
        )
        for repetition in range(3)
    }


def test_p8_frozen_two_repeat_decision_drives_schedule_and_plan(
    frozen,
    tmp_path: Path,
) -> None:
    decision = frozen.repeat_decision.model_copy(update={"selected_repeats": 2})
    protocol_path, decision_path = _write_package7_artifacts(
        tmp_path,
        frozen.protocol,
        decision,
    )
    frozen_two = load_frozen_package7_heldout_inputs_v3(
        project_root=PROJECT_ROOT,
        protocol_path=protocol_path,
        repeat_decision_path=decision_path,
        gold_path=PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json",
        split_path=PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json",
    )

    first = build_heldout_schedule_v3(frozen_two, run_id="run_p8_two_repeats")
    second = build_heldout_schedule_v3(frozen_two, run_id="run_p8_two_repeats")
    first_plan = build_heldout_execution_plan_v3(
        frozen_two,
        first,
        run_id="run_p8_two_repeats",
    )
    second_plan = build_heldout_execution_plan_v3(
        frozen_two,
        second,
        run_id="run_p8_two_repeats",
    )

    assert first == second
    assert pilot_schedule_sha256_v3(first) == pilot_schedule_sha256_v3(second)
    assert len(first) == 480
    assert [turn.schedule_index for turn in first] == list(range(480))
    assert [turn.execution_order for turn in first] == list(range(480))
    assert {
        (turn.identity.case_id, turn.identity.variant_id, turn.identity.repetition)
        for turn in first
    } == {
        (case_id, variant_id, repetition)
        for case_id in frozen_two.heldout_cases
        for variant_id in PACKAGE7_VARIANT_ORDER
        for repetition in range(2)
    }
    assert first_plan == second_plan
    assert first_plan.selected_repeats == 2
    assert first_plan.measured_cell_count == 480
    validate_heldout_execution_plan_v3(first_plan, second_plan)

    tampered_plan = first_plan.model_copy(
        update={"selected_repeats": 3, "measured_cell_count": 720}
    )
    with pytest.raises(FrozenPackage7DriftError, match="package8_execution_plan_drift"):
        validate_heldout_execution_plan_v3(tampered_plan, first_plan)


def test_p8_rejects_an_invalid_repeat_decision(
    frozen,
    tmp_path: Path,
) -> None:
    invalid_decision = frozen.repeat_decision.model_copy(update={"selected_repeats": 4})
    protocol_path, decision_path = _write_package7_artifacts(
        tmp_path,
        frozen.protocol,
        invalid_decision,
    )

    with pytest.raises(FrozenPackage7DriftError, match="repeat_decision_invalid"):
        load_frozen_package7_heldout_inputs_v3(
            project_root=PROJECT_ROOT,
            protocol_path=protocol_path,
            repeat_decision_path=decision_path,
            gold_path=PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json",
            split_path=PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json",
        )

    tampered_frozen = replace(frozen, repeat_decision=invalid_decision)
    with pytest.raises(FrozenPackage7DriftError, match="repeat_decision_drift"):
        build_heldout_schedule_v3(tampered_frozen, run_id="run_p8_invalid_repeat")


def test_p8_adapts_only_heldout_cases_with_exact_bindings_and_fixtures(frozen) -> None:
    entries = tuple(
        entry
        for entry in frozen.loaded_gold.split.entries
        if entry.split is EvaluationSplitV3.HELD_OUT
    )
    conversations = {
        item.conversation_id: item for item in frozen.loaded_gold.gold.conversations
    }

    assert tuple(frozen.heldout_cases) == tuple(
        entry.conversation_id for entry in entries
    )
    assert len(frozen.heldout_cases) == 60
    assert not set(frozen.heldout_cases) & {
        case.case_id for case in frozen.protocol.experiment.pilot_cases
    }
    for entry in entries:
        case = frozen.heldout_cases[entry.conversation_id]
        gold_case = conversations[entry.conversation_id]
        assert case.work_group_id == entry.work_group_id == gold_case.work_group_id
        assert case.resolved_product_ids == gold_case.product_ids
        if gold_case.sandbox_fixture is None:
            assert case.sandbox_fixture is None
        else:
            assert case.sandbox_fixture is not None
            assert (
                case.sandbox_fixture.fixture_id == gold_case.sandbox_fixture.fixture_id
            )
            assert case.sandbox_fixture.fixture_sha256 == entry.sandbox_fixture_sha256


def test_p8_refuses_a_drifted_package7_protocol_before_schedule_construction(
    frozen,
    tmp_path: Path,
) -> None:
    payload = frozen.protocol.model_dump(mode="json")
    protocol_path, decision_path = _write_package7_artifacts(
        tmp_path,
        frozen.protocol,
        frozen.repeat_decision,
    )
    payload["assets"]["pricing_sha256"] = "f" * 64
    protocol_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FrozenPackage7DriftError, match="package7_protocol_hash_drift"):
        load_frozen_package7_heldout_inputs_v3(
            project_root=PROJECT_ROOT,
            protocol_path=protocol_path,
            repeat_decision_path=decision_path,
            gold_path=PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json",
            split_path=PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json",
        )


def test_p8_cli_validate_and_dry_run_are_local_and_run_requires_consent(
    frozen,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    protocol_path, decision_path = _write_package7_artifacts(
        tmp_path,
        frozen.protocol,
        frozen.repeat_decision,
    )
    common = (
        "--project-root",
        str(PROJECT_ROOT),
        "--p7-protocol",
        str(protocol_path),
        "--p7-repeat-decision",
        str(decision_path),
    )
    assert benchmark_cli.cli(("validate", *common)) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["status"] == "valid"
    assert validation["heldout_cases"] == 60

    assert benchmark_cli.cli(("dry-run", *common, "--run-id", "run_p8_dry")) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["scheduled"] == dry_run["measurements"] == 720
    assert dry_run["warmups"] == dry_run["network_calls"] == 0

    output = tmp_path / "heldout"
    assert benchmark_cli.cli(("run", *common, "--output", str(output))) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error"] == "network_consent_required"
    assert not output.exists()


@pytest.mark.asyncio
async def test_p8_completed_cells_are_not_replayed_on_resume(
    frozen,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_schedule = build_heldout_schedule_v3(frozen, run_id="run_p8_completed")
    schedule = (full_schedule[0],)
    single_case = frozen.heldout_cases[schedule[0].identity.case_id]
    single_case_frozen = replace(
        frozen,
        heldout_cases={single_case.case_id: single_case},
    )
    monkeypatch.setattr(
        benchmark_v3,
        "validate_heldout_schedule_v3",
        lambda *args, **kwargs: None,
    )
    checkpoint = tmp_path / "heldout-checkpoint.v3.jsonl"
    first_factory = _RecordingFactory()
    first = await run_heldout_benchmark_v3(
        frozen=single_case_frozen,
        schedule=schedule,
        checkpoint_path=checkpoint,
        executor_factory=first_factory,
    )
    assert first.summary.completed_turn_ids == (schedule[0].turn_id,)
    assert first_factory.calls == 1

    forbidden = _ForbiddenFactory()
    resumed = await run_heldout_benchmark_v3(
        frozen=single_case_frozen,
        schedule=schedule,
        checkpoint_path=checkpoint,
        executor_factory=forbidden,
    )
    assert resumed.summary.completed_turn_ids == (schedule[0].turn_id,)
    assert forbidden.calls == 0
    records = checkpoint.read_text(encoding="utf-8").splitlines()
    assert len(records) == 2


def test_p8_orphaned_start_is_sealed_as_ambiguous_without_dispatch(
    frozen,
    tmp_path: Path,
) -> None:
    schedule = build_heldout_schedule_v3(frozen, run_id="run_p8_orphan")
    checkpoint = tmp_path / "heldout-checkpoint.v3.jsonl"
    runner = HeldoutEvaluationV3ObservationRunner(
        frozen=frozen,
        schedule=schedule,
        checkpoint_path=checkpoint,
        executor_factory=_ForbiddenFactory(),
    )
    with CheckpointWriterV3(
        checkpoint,
        run_id=runner.run_id,
        protocol_sha256=frozen.protocol_sha256,
        execution_case_set_sha256=runner.execution_case_set_sha256,
        schedule_sha256=runner.schedule_sha256,
        schedule=schedule,
    ) as writer:
        writer.append_started(schedule[0])

    result = runner.seal_orphan_started_observations()
    assert result.summary.ambiguous_turn_ids == (schedule[0].turn_id,)
    assert result.summary.orphan_started_turn_ids == ()
    assert result.summary.pending_turn_ids[0] == schedule[1].turn_id


class _RecordingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> _RecordingExecutor:
        self.calls += 1
        return _RecordingExecutor(context, case)


class _RecordingExecutor:
    def __init__(
        self,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> None:
        self.context = context
        self.case = case

    async def reset_initial_state(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> InitialStateResetReceiptV3:
        assert context == self.context
        assert case == self.case
        return initial_state_reset_receipt_v3(context, case)

    async def execute_turn(
        self,
        request: UserTurnExecutionRequestV3,
    ) -> UserTurnExecutionResultV3:
        digest = canonical_sha256(
            {
                "execution_turn_id": request.attribution.execution_turn_id,
                "source_turn_id": request.attribution.source_turn_id,
            }
        )
        return UserTurnExecutionResultV3(
            attribution=request.attribution,
            result_payload={"status": "completed"},
            model_calls=(
                ModelCallEvidenceV3(
                    call_id=f"mcall_{digest}",
                    attribution=request.attribution,
                    model="gpt-5.4-mini-2026-03-17",
                    attempts=1,
                    input_tokens=1,
                    cached_input_tokens=0,
                    output_tokens=1,
                    reasoning_tokens=0,
                    total_tokens=2,
                ),
            ),
            ledger_events=(
                LedgerEventEvidenceV3(
                    ledger_event_id=f"ledger_{digest}",
                    attribution=request.attribution,
                    kind=LedgerEventKindV3.SETTLED_KNOWN,
                    known_cost_usd=Decimal("0.001"),
                ),
            ),
            peak_provider_concurrency=1,
            max_attempt_duration_seconds=0.01,
            elapsed_seconds=0.02,
        )


class _ForbiddenFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> ObservationExecutorV3:
        del context, case
        self.calls += 1
        raise AssertionError("a terminal or orphaned held-out cell was redispatched")


def _write_package7_artifacts(
    directory: Path,
    protocol: EvaluationProtocolV3,
    decision: RepeatDecisionV3,
) -> tuple[Path, Path]:
    protocol_path = directory / "protocol.v3.json"
    decision_path = directory / "repeat-decision.v3.json"
    protocol_path.write_bytes(canonical_json_bytes(protocol))
    decision_path.write_bytes(canonical_json_bytes(decision))
    return protocol_path, decision_path
