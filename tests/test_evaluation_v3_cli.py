"""Executable Package 7 pilot, resume, and repeat-freeze CLI tests."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.evaluation import v3_cli
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_comparison import ArtifactBindingsV3
from app.evaluation.v3_judge import build_model_judge_configuration_v3
from app.evaluation.v3_models import EvaluationProtocolV3, RepeatDecisionV3
from app.evaluation.v3_protocol import evaluation_protocol_sha256_v3
from app.evaluation.v3_runner import (
    EmbeddingCallEvidenceV3,
    EvaluationCaseV3,
    InitialStateResetReceiptV3,
    LedgerEventEvidenceV3,
    LedgerEventKindV3,
    ModelCallEvidenceV3,
    ObservationExecutionContextV3,
    UserTurnExecutionRequestV3,
    UserTurnExecutionResultV3,
    initial_state_reset_receipt_v3,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = "postgresql+psycopg://evaluation@example.invalid/package7"


class _ControlledFactory:
    def __init__(self, *, known_cost: Decimal = Decimal("0.001")) -> None:
        self.known_cost = known_cost
        self.calls = 0

    def __call__(
        self,
        *,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> _ControlledExecutor:
        self.calls += 1
        return _ControlledExecutor(self, context, case)


class _ControlledExecutor:
    def __init__(
        self,
        factory: _ControlledFactory,
        context: ObservationExecutionContextV3,
        case: EvaluationCaseV3,
    ) -> None:
        self.factory = factory
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
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=5,
                    reasoning_tokens=1,
                    total_tokens=15,
                ),
            ),
            embedding_calls=(
                EmbeddingCallEvidenceV3(
                    call_id=f"ecall_{digest}",
                    attribution=request.attribution,
                    model="text-embedding-3-small",
                    attempts=1,
                    input_tokens=3,
                ),
            ),
            ledger_events=(
                LedgerEventEvidenceV3(
                    ledger_event_id=f"ledger_{digest}",
                    attribution=request.attribution,
                    kind=LedgerEventKindV3.SETTLED_KNOWN,
                    known_cost_usd=self.factory.known_cost,
                ),
            ),
            peak_provider_concurrency=1,
            max_attempt_duration_seconds=0.01,
            elapsed_seconds=0.02,
        )


class _ForbiddenFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **_: object) -> None:
        self.calls += 1
        raise AssertionError("resume dispatched a terminal observation")


def _base_args(output: Path, command: str) -> tuple[str, ...]:
    return (
        command,
        "--project-root",
        str(PROJECT_ROOT),
        "--output",
        str(output),
        "--run-id",
        "run_cli_package7_v3",
    )


def test_validate_and_dry_run_are_local_and_reproduce_exact_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = set(tmp_path.iterdir())

    def forbidden(_: object) -> None:
        raise AssertionError("local validation constructed a live runtime")

    monkeypatch.setattr(v3_cli, "_build_live_executor_factory", forbidden)

    assert v3_cli.cli(("validate", "--project-root", str(PROJECT_ROOT))) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["status"] == "valid"
    assert validation["pilot_cases"] == 8
    assert validation["variants"] == 4

    arguments = (
        "dry-run",
        "--project-root",
        str(PROJECT_ROOT),
        "--run-id",
        "run_cli_dry_v3",
    )
    assert v3_cli.cli(arguments) == v3_cli.cli(arguments) == 0
    first, second = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert first == second
    assert first["status"] == "dry_run"
    assert first["scheduled"] == 36
    assert first["warmups"] == 4
    assert first["measurements"] == 32
    assert first["network_calls"] == 0
    assert set(tmp_path.iterdir()) == before


def test_pilot_requires_network_consent_before_artifact_or_runtime_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "pilot"

    def forbidden(_: object) -> None:
        raise AssertionError("runtime constructed without consent")

    monkeypatch.setattr(v3_cli, "_build_live_executor_factory", forbidden)

    assert v3_cli.cli(_base_args(output, "pilot")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"] == "network_consent_required"
    assert not output.exists()


def test_pilot_requires_explicit_database_url_before_artifacts_or_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "pilot"

    def forbidden(_: object) -> None:
        raise AssertionError("runtime constructed without an explicit database URL")

    monkeypatch.setattr(v3_cli, "_build_live_executor_factory", forbidden)

    assert v3_cli.cli((*_base_args(output, "pilot"), "--allow-network")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"] == "database_url_required"
    assert not output.exists()


def test_runtime_settings_require_durable_postgres_before_service_resolution() -> None:
    inputs = v3_cli._load_inputs(
        v3_cli._parser().parse_args(("validate", "--project-root", str(PROJECT_ROOT)))
    )
    snapshot = inputs.knowledge_snapshot
    settings = SimpleNamespace(
        database_url="sqlite+pysqlite:///local.db",
        model_runtime_mode="required",
        openai_planning_model="gpt-5.4-mini-2026-03-17",
        openai_specialist_model="gpt-5.4-mini-2026-03-17",
        openai_synthesis_model="gpt-5.4-mini-2026-03-17",
        embedding_backend="openai",
        openai_embedding_model=snapshot.embedding_model,
        openai_embedding_dimensions=snapshot.embedding_dimension,
        v2_corpus_version_id=snapshot.corpus_version_id,
        v2_index_manifest_id=snapshot.index_manifest_id,
    )

    with pytest.raises(
        v3_cli.EvaluationV3CLIError,
        match="durable PostgreSQL",
    ) as captured:
        v3_cli._validate_runtime_settings(settings, snapshot)
    assert captured.value.code == "runtime_database_not_postgresql"


def test_controlled_pilot_resumes_without_dispatch_and_freezes_global_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "pilot"
    controlled = _ControlledFactory(known_cost=Decimal("0.05"))
    monkeypatch.setattr(
        v3_cli,
        "_build_live_executor_factory",
        lambda _: controlled,
    )
    pilot_args = (
        *_base_args(output, "pilot"),
        "--allow-network",
        "--database-url",
        TEST_DATABASE_URL,
    )

    assert v3_cli.cli(pilot_args) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "complete"
    assert completed["scheduled"] == completed["completed"] == 36
    assert completed["failed"] == completed["ambiguous"] == completed["missing"] == 0
    assert controlled.calls == 36
    checkpoint = output / "pilot-checkpoint.v3.jsonl"
    before = checkpoint.read_bytes()
    assert len(checkpoint.read_text(encoding="utf-8").splitlines()) == 72

    forbidden = _ForbiddenFactory()
    monkeypatch.setattr(
        v3_cli,
        "_build_live_executor_factory",
        lambda _: forbidden,
    )
    resume_args = _base_args(output, "resume")
    assert v3_cli.cli(resume_args) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "complete"
    assert forbidden.calls == 0
    assert checkpoint.read_bytes() == before

    assert v3_cli.cli(_base_args(output, "freeze")) == 0
    frozen = json.loads(capsys.readouterr().out)
    assert frozen["status"] == "frozen"
    assert frozen["selected_repeats"] == 2
    decision_path = output / "repeat-decision.v3.json"
    decision = RepeatDecisionV3.model_validate_json(
        decision_path.read_text(encoding="utf-8")
    )
    assert decision.selected_repeats == 2
    decision_before = decision_path.read_bytes()

    assert v3_cli.cli(_base_args(output, "repeat-freeze")) == 0
    capsys.readouterr()
    assert decision_path.read_bytes() == decision_before


def test_resume_rejects_protocol_drift_before_live_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "pilot"
    controlled = _ControlledFactory()
    monkeypatch.setattr(
        v3_cli,
        "_build_live_executor_factory",
        lambda _: controlled,
    )
    assert (
        v3_cli.cli(
            (
                *_base_args(output, "pilot"),
                "--allow-network",
                "--database-url",
                TEST_DATABASE_URL,
            )
        )
        == 0
    )
    capsys.readouterr()

    protocol_path = output / "protocol.v3.json"
    protocol = EvaluationProtocolV3.model_validate_json(
        protocol_path.read_text(encoding="utf-8")
    )
    changed = protocol.model_copy(
        update={
            "assets": protocol.assets.model_copy(update={"pricing_sha256": "f" * 64})
        }
    )
    protocol_path.write_text(changed.model_dump_json(), encoding="utf-8")
    forbidden = _ForbiddenFactory()
    monkeypatch.setattr(
        v3_cli,
        "_build_live_executor_factory",
        lambda _: forbidden,
    )

    assert v3_cli.cli((*_base_args(output, "resume"), "--allow-network")) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "protocol_drift"
    assert forbidden.calls == 0


def test_environment_failure_is_partial_and_does_not_expose_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "pilot"
    secret = "postgresql+psycopg://user:secret@example.invalid/raw-prompt"

    def unavailable(inputs: v3_cli.EvaluationV3CLIInputs) -> None:
        assert inputs.database_url == secret
        raise RuntimeError(secret)

    monkeypatch.setattr(v3_cli, "_build_live_executor_factory", unavailable)

    exit_code = v3_cli.cli(
        (
            *_base_args(output, "pilot"),
            "--allow-network",
            "--database-url",
            secret,
        )
    )
    encoded = capsys.readouterr().out
    payload = json.loads(encoded)
    assert exit_code == 3
    assert payload["status"] == "partial"
    assert payload["error"] == "environment_unavailable"
    assert payload["missing"] == 36
    assert secret not in encoded


def test_cli_protocol_builds_a_valid_model_judge_configuration() -> None:
    inputs = v3_cli._load_inputs(
        v3_cli._parser().parse_args(("validate", "--project-root", str(PROJECT_ROOT)))
    )
    protocol = inputs.protocol
    assets = protocol.assets
    model_binding = protocol.experiment.judgment_policy.model_binding
    assert model_binding is not None
    assert assets.judge_prompt_sha256 != assets.prompt_bundle_sha256

    bindings = ArtifactBindingsV3(
        protocol_sha256=evaluation_protocol_sha256_v3(protocol),
        gold_sha256=assets.gold_sha256,
        split_sha256=assets.split_sha256,
        schedule_sha256="a" * 64,
        repeat_decision_sha256="b" * 64,
        evaluator_configuration_sha256=assets.evaluator_configuration_sha256,
        judge_prompt_sha256=assets.judge_prompt_sha256,
        judge_schema_sha256=assets.judge_schema_sha256,
        tool_contract_sha256=assets.tool_contract_sha256,
        corpus_sha256=assets.corpus_sha256,
        index_sha256=assets.index_sha256,
        embedding_model_dimensions_sha256=(assets.embedding_model_dimensions_sha256),
        pricing_manifest_sha256=assets.pricing_manifest_sha256,
        usage_ledger_sha256=assets.usage_ledger_sha256,
        rubric_sha256=assets.rubric_sha256,
    )

    configuration = build_model_judge_configuration_v3(
        bindings=bindings,
        model_binding=model_binding,
        judge_prompt=v3_cli.PACKAGE7_JUDGE_PROMPT_V3,
    )

    assert (
        configuration.bindings.evaluator_configuration_sha256
        == assets.evaluator_configuration_sha256
    )
