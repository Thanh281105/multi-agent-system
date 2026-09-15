"""Contract tests for the additive durable Package 8 model-judge runner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.benchmark_judging import (
    EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3,
    DurableModelJudgeRunnerV3,
    FrozenJudgeRunError,
    JudgeBudgetPolicyV3,
    JudgeJobIdentityV3,
    JudgeJournalV3,
    build_heldout_judge_run_key_v3,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import (
    BlindedAnswerPacketV3,
    BlindedAnswerV3,
    CitationForReviewV3,
    RubricContextV3,
    RubricFactV3,
)
from app.evaluation.v3_comparison import ArtifactBindingsV3
from app.evaluation.v3_gold import (
    AnswerabilityV3,
    EvaluationSplitV3,
    RequiredResponseModeV3,
)
from app.evaluation.v3_judge import (
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationFreezeV3,
    CalibrationReferenceBundleV3,
    CalibrationReferenceLabelsV3,
    CalibrationThresholdsV3,
    DevelopmentCalibrationCaseV3,
    ModelJudgeConfigurationV3,
    build_calibration_reference_bundle_v3,
    build_calibration_reference_labels_v3,
    build_calibration_thresholds_v3,
    build_development_calibration_case_v3,
    build_model_judge_configuration_v3,
    evaluator_configuration_sha256_v3,
    freeze_calibration_thresholds_v3,
    judge_prompt_sha256_v3,
    model_judge_output_schema_sha256_v3,
    run_development_calibration_case_v3,
)
from app.evaluation.v3_models import (
    EvaluationMetricV3,
    GenerationBindingV3,
)
from app.shared.budget import current_provider_budget

PROMPT = "Score the blinded answer against the supplied frozen rubric only."
MODEL = GenerationBindingV3(model="gpt-5.4-mini-2026-03-17")
SOURCE_MANIFEST = "b" * 64


@pytest.fixture
def context() -> tuple[
    ModelJudgeConfigurationV3,
    BlindedAnswerV3,
    BlindedAnswerPacketV3,
    DevelopmentCalibrationCaseV3,
    CalibrationReferenceLabelsV3,
    CalibrationReferenceBundleV3,
    CalibrationThresholdsV3,
    CalibrationFreezeV3,
]:
    bindings = _bindings()
    configuration = build_model_judge_configuration_v3(
        bindings=bindings,
        model_binding=MODEL,
        judge_prompt=PROMPT,
    )
    answer = _answer("answer_000000000000000000000001")
    packet = _packet(bindings, (answer,))
    case = build_development_calibration_case_v3(
        calibration_id="calibration_case_one",
        answer=answer,
    )
    thresholds = build_calibration_thresholds_v3(
        minimum_development_cases=1,
        maximum_absolute_error=_scores(0.0),
    )
    reference = build_calibration_reference_labels_v3(
        case,
        development_observation_id="development_observation_one",
        source_classification="automated_gold_derived",
        scores=_scores(1.0),
    )
    references = build_calibration_reference_bundle_v3(
        protocol_sha256=configuration.bindings.protocol_sha256,
        references=(reference,),
    )
    calibration_record = run_development_calibration_case_v3(
        configuration,
        case,
        reference=reference,
        thresholds=thresholds,
        judge=lambda request: _output(request.answer),
    )
    freeze = freeze_calibration_thresholds_v3(
        configuration,
        (calibration_record,),
        thresholds,
        references,
        expected_calibration_ids=(case.calibration_id,),
    )
    return (
        configuration,
        answer,
        packet,
        case,
        reference,
        references,
        thresholds,
        freeze,
    )


@pytest.mark.asyncio
async def test_runner_is_blind_budgeted_and_replays_completed_jobs_without_egress(
    context: tuple[Any, ...],
    tmp_path: Path,
) -> None:
    configuration, answer, packet, _, _, _, _, freeze = context
    ledger = _RecordingLedger()
    runtime = _FakeRuntime(ledger)
    runner = _runner(runtime, ledger, tmp_path / "heldout.journal.jsonl")

    first = await runner.run_heldout_packet(
        packet=packet,
        configuration=configuration,
        calibration=freeze,
    )

    assert len(first) == 1
    assert first[0].status == "completed"
    assert first[0].record is not None
    assert first[0].record.opaque_answer_id == answer.opaque_answer_id
    assert first[0].ledger_attempts == (
        {
            "scope_id": first[0].scope_id,
            "purpose": "judge",
            "reservation": "recorded",
            "attempt_timeout_seconds": 18.0,
        },
    )
    assert len(runtime.requests) == 1
    assert len(ledger.created_scopes) == 1
    _, scope = next(iter(ledger.created_scopes.items()))
    assert scope == {
        "account_id": "judge-account",
        "purpose": "judge",
        "hard_limit_nano_usd": 1_000,
        "max_generation_calls": 1,
        "max_provider_attempts": 2,
        "max_concurrency": 1,
    }
    request = runtime.requests[0]
    assert request["phase"] == "held_out_scoring"
    assert request["answer"] == answer.model_dump(mode="json")
    serialized = json.dumps(request, sort_keys=True)
    for forbidden in (
        "unblinding",
        "variant_id",
        "work_group_id",
        "observation_id",
        "conversation_id",
    ):
        assert forbidden not in serialized

    resumed = await runner.run_heldout_packet(
        packet=packet,
        configuration=configuration,
        calibration=freeze,
    )

    assert resumed[0].status == "completed"
    assert resumed[0].replayed is True
    assert len(runtime.requests) == 1
    assert len(ledger.created_scopes) == 1


@pytest.mark.asyncio
async def test_orphaned_start_is_permanently_ambiguous_without_dispatch(
    context: tuple[Any, ...],
    tmp_path: Path,
) -> None:
    configuration, answer, packet, _, _, _, _, freeze = context
    ledger = _RecordingLedger()
    runtime = _FakeRuntime(ledger)
    journal_path = tmp_path / "orphan.journal.jsonl"
    key = build_heldout_judge_run_key_v3(
        packet=packet,
        configuration=configuration,
        calibration=freeze,
        additive_source_manifest_sha256=SOURCE_MANIFEST,
    )
    identity = JudgeJobIdentityV3(
        phase="held_out_scoring",
        target_id=answer.opaque_answer_id,
        source_sha256=canonical_sha256(answer),
        run_key_sha256=key.run_key_sha256,
    )
    with JudgeJournalV3(journal_path, run_key=key) as journal:
        journal.append_started(identity, scope_id="judge_orphaned_cell")

    result = await _runner(runtime, ledger, journal_path).run_heldout_packet(
        packet=packet,
        configuration=configuration,
        calibration=freeze,
    )

    assert result[0].status == "ambiguous"
    assert result[0].replayed is True
    assert result[0].error_code == "orphaned_started_job"
    assert runtime.requests == []
    assert ledger.created_scopes == {}


@pytest.mark.asyncio
async def test_invalid_or_fabricated_model_output_becomes_terminal_failure(
    context: tuple[Any, ...],
    tmp_path: Path,
) -> None:
    configuration, _, packet, _, _, _, _, freeze = context
    ledger = _RecordingLedger()
    runtime = _FakeRuntime(ledger, fabricated_citation=True)
    runner = _runner(runtime, ledger, tmp_path / "invalid-output.journal.jsonl")

    result = await runner.run_heldout_packet(
        packet=packet,
        configuration=configuration,
        calibration=freeze,
    )

    assert result[0].status == "failed"
    assert result[0].record is None
    assert result[0].error_code == "judge_output_contract_rejected"
    assert result[0].ledger_attempts
    assert len(runtime.requests) == 1

    replay = await runner.run_heldout_packet(
        packet=packet,
        configuration=configuration,
        calibration=freeze,
    )
    assert replay[0].status == "failed"
    assert replay[0].replayed is True
    assert len(runtime.requests) == 1


@pytest.mark.asyncio
async def test_development_runner_records_calibration_provenance_and_budget_scope(
    context: tuple[Any, ...],
    tmp_path: Path,
) -> None:
    configuration, _, _, case, reference, references, thresholds, _ = context
    ledger = _RecordingLedger()
    runtime = _FakeRuntime(ledger)
    result = await _runner(
        runtime, ledger, tmp_path / "development.journal.jsonl"
    ).run_development_calibration_cases(
        configuration=configuration,
        cases=(case,),
        references=references,
        thresholds=thresholds,
    )

    assert result[0].status == "completed"
    assert result[0].record is not None
    calibration_record = result[0].record
    assert calibration_record.calibration_id == case.calibration_id
    assert calibration_record.configuration_sha256 == configuration.configuration_sha256
    assert calibration_record.reference_sha256 == reference.reference_sha256
    assert calibration_record.thresholds_sha256 == thresholds.thresholds_sha256
    assert runtime.requests[0]["phase"] == "development_calibration"
    assert runtime.requests[0]["calibration_sha256"] is None


@pytest.mark.asyncio
async def test_provenance_mismatch_fails_closed_before_provider_execution(
    context: tuple[Any, ...],
    tmp_path: Path,
) -> None:
    configuration, _, packet, _, _, _, _, freeze = context
    ledger = _RecordingLedger()
    runtime = _FakeRuntime(ledger)
    incompatible = _freeze_with_configuration("e" * 64)
    runner = _runner(runtime, ledger, tmp_path / "mismatch.journal.jsonl")

    with pytest.raises(FrozenJudgeRunError, match="calibration_configuration_mismatch"):
        await runner.run_heldout_packet(
            packet=packet,
            configuration=configuration,
            calibration=incompatible,
        )

    assert runtime.requests == []
    assert ledger.created_scopes == {}
    assert not (tmp_path / "mismatch.journal.jsonl").exists()


@pytest.mark.asyncio
async def test_calibration_reference_provenance_mismatch_fails_closed(
    context: tuple[Any, ...],
    tmp_path: Path,
) -> None:
    configuration, _, _, case, _, _, thresholds, _ = context
    alternate_case = build_development_calibration_case_v3(
        calibration_id=case.calibration_id,
        answer=_answer("answer_0000000000000000000000ff"),
    )
    alternate_reference = build_calibration_reference_labels_v3(
        alternate_case,
        development_observation_id="development_observation_alternate",
        source_classification="automated_gold_derived",
        scores=_scores(1.0),
    )
    alternate_references = build_calibration_reference_bundle_v3(
        protocol_sha256=configuration.bindings.protocol_sha256,
        references=(alternate_reference,),
    )
    ledger = _RecordingLedger()
    runtime = _FakeRuntime(ledger)

    with pytest.raises(
        FrozenJudgeRunError, match="calibration_reference_case_mismatch"
    ):
        await _runner(
            runtime, ledger, tmp_path / "calibration-mismatch.journal.jsonl"
        ).run_development_calibration_cases(
            configuration=configuration,
            cases=(case,),
            references=alternate_references,
            thresholds=thresholds,
        )

    assert runtime.requests == []
    assert ledger.created_scopes == {}


def _runner(
    runtime: _FakeRuntime,
    ledger: _RecordingLedger,
    journal_path: Path,
) -> DurableModelJudgeRunnerV3:
    return DurableModelJudgeRunnerV3(
        runtime=runtime,
        ledger=ledger,
        account_id="judge-account",
        journal_path=journal_path,
        additive_source_manifest_sha256=SOURCE_MANIFEST,
        budget_policy=JudgeBudgetPolicyV3(hard_limit_nano_usd=1_000),
    )


class _RecordingLedger:
    def __init__(self) -> None:
        self.created_scopes: dict[str, dict[str, object]] = {}
        self.attempts: dict[str, list[dict[str, object]]] = {}

    def create_scope(self, **kwargs: object) -> None:
        scope_id = str(kwargs.pop("scope_id"))
        if scope_id in self.created_scopes:
            raise AssertionError("a completed judge job tried to create another scope")
        self.created_scopes[scope_id] = dict(kwargs)
        self.attempts[scope_id] = []

    def reserve(self, scope_id: str) -> None:
        self.attempts[scope_id].append(
            {
                "scope_id": scope_id,
                "purpose": "judge",
                "reservation": "recorded",
                "attempt_timeout_seconds": 18.0,
            }
        )

    def scope_attempt_snapshots(self, scope_id: str) -> tuple[object, ...]:
        return tuple(self.attempts.get(scope_id, ()))


@dataclass(frozen=True)
class _RuntimeResult:
    value: object


class _FakeRuntime:
    def __init__(
        self, ledger: _RecordingLedger, *, fabricated_citation: bool = False
    ) -> None:
        self.ledger = ledger
        self.fabricated_citation = fabricated_citation
        self.requests: list[dict[str, object]] = []

    async def generate_structured(self, **kwargs: Any) -> _RuntimeResult:
        budget = current_provider_budget()
        assert budget is not None
        assert budget.ledger is self.ledger
        assert budget.purpose == "judge"
        assert budget.max_retries == 1
        assert budget.attempt_timeout_seconds == 18.0
        assert kwargs["schema"].__name__ == "ModelJudgeOutputV3"
        request = json.loads(kwargs["input_text"])
        self.requests.append(request)
        self.ledger.reserve(budget.scope_id)
        answer = BlindedAnswerV3.model_validate(request["answer"])
        return _RuntimeResult(
            _output(answer, fabricated_citation=self.fabricated_citation)
        )


def _bindings() -> ArtifactBindingsV3:
    prompt_hash = judge_prompt_sha256_v3(PROMPT)
    schema_hash = model_judge_output_schema_sha256_v3()
    rubric_sha256 = "d" * 64
    evaluator_sha256 = evaluator_configuration_sha256_v3(
        model_binding=MODEL,
        judge_prompt_sha256=prompt_hash,
        judge_schema_sha256=schema_hash,
        rubric_sha256=rubric_sha256,
    )
    return ArtifactBindingsV3(
        protocol_sha256=EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3,
        gold_sha256="1" * 64,
        split_sha256="2" * 64,
        schedule_sha256="3" * 64,
        repeat_decision_sha256="4" * 64,
        evaluator_configuration_sha256=evaluator_sha256,
        judge_prompt_sha256=prompt_hash,
        judge_schema_sha256=schema_hash,
        tool_contract_sha256="5" * 64,
        corpus_sha256="6" * 64,
        index_sha256="7" * 64,
        embedding_model_dimensions_sha256="8" * 64,
        pricing_manifest_sha256="9" * 64,
        usage_ledger_sha256="a" * 64,
        rubric_sha256=rubric_sha256,
    )


def _answer(opaque_answer_id: str) -> BlindedAnswerV3:
    evidence = "Frozen evidence for a required fact."
    return BlindedAnswerV3(
        opaque_answer_id=opaque_answer_id,
        prompt=("A blinded prompt.",),
        answer="A blinded cited answer.",
        citations=(CitationForReviewV3(label="source-1", evidence=evidence),),
        rubric_context=RubricContextV3(
            answerability=AnswerabilityV3.ANSWERABLE,
            required_response_mode=RequiredResponseModeV3.DIRECT_ANSWER,
            required_facts=(
                RubricFactV3(
                    claim="A required fact",
                    expected_value=True,
                    evidence=evidence,
                ),
            ),
            forbidden_claims=(),
            expected_action_outcome="complete",
        ),
    )


def _packet(
    bindings: ArtifactBindingsV3,
    answers: tuple[BlindedAnswerV3, ...],
) -> BlindedAnswerPacketV3:
    payload = {
        "schema_version": "3.0",
        "split": EvaluationSplitV3.HELD_OUT,
        "bindings": bindings.model_dump(mode="json"),
        "randomization_seed": 42,
        "answer_count": len(answers),
        "answers": [item.model_dump(mode="json") for item in answers],
    }
    return BlindedAnswerPacketV3(
        bindings=bindings,
        randomization_seed=42,
        answer_count=len(answers),
        answers=answers,
        packet_sha256=canonical_sha256(payload),
    )


def _scores(value: float) -> dict[EvaluationMetricV3, float]:
    return {metric: value for metric in SEMANTIC_JUDGE_METRICS_V3}


def _output(
    answer: BlindedAnswerV3,
    *,
    fabricated_citation: bool = False,
) -> dict[str, object]:
    citation = "invented-source" if fabricated_citation else "source-1"
    return {
        "schema_version": "3.0",
        "output_schema_sha256": model_judge_output_schema_sha256_v3(),
        "opaque_answer_id": answer.opaque_answer_id,
        "verdicts": [
            {
                "metric": metric,
                "score": 1.0,
                "rubric_fact_indices": [0],
                "citation_labels": [citation],
            }
            for metric in SEMANTIC_JUDGE_METRICS_V3
        ],
    }


def _freeze_with_configuration(configuration_sha256: str) -> CalibrationFreezeV3:
    payload = {
        "schema_version": "3.0",
        "protocol_sha256": EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3,
        "configuration_sha256": configuration_sha256,
        "thresholds_sha256": "1" * 64,
        "reference_bundle_sha256": "2" * 64,
        "expected_calibration_ids": ("calibration_case_one",),
        "development_record_sha256s": ("3" * 64,),
        "maximum_observed_errors": _scores(0.0),
    }
    return CalibrationFreezeV3(
        protocol_sha256=EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3,
        configuration_sha256=configuration_sha256,
        thresholds_sha256="1" * 64,
        reference_bundle_sha256="2" * 64,
        expected_calibration_ids=("calibration_case_one",),
        development_record_sha256s=("3" * 64,),
        maximum_observed_errors=_scores(0.0),
        calibration_sha256=canonical_sha256(payload),
    )
