"""Independent per-claim calibration and fail-closed successor boundaries."""

from __future__ import annotations

from typing import Any

import pytest

from app.evaluation.benchmark_calibration import build_independent_reference_labels_v3
from app.evaluation.benchmark_judging import (
    EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3,
    FrozenJudgeRunError,
    JudgeJobIdentityV3,
    _terminal_result,
    build_heldout_judge_run_key_v3,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_artifacts import ClaimCitationVerdictV3, ClaimEvidenceV3
from app.evaluation.v3_calibration import (
    build_calibration_record_collection_v3,
    validate_calibration_artifacts_v3,
    write_calibration_artifacts_v3,
)
from app.evaluation.v3_judge import (
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationFreezeV3,
    CalibrationRecordV3,
    ReferenceAdjudicatorV3,
    build_calibration_record_v3,
    build_calibration_reference_bundle_v3,
    build_calibration_reference_labels_v3,
    build_calibration_thresholds_v3,
    build_development_calibration_case_v3,
    build_model_judge_configuration_v3,
    freeze_calibration_thresholds_v3,
    judge_blinded_packet_v3,
    run_development_calibration_case_v3,
    score_runtime_metrics_v3,
    validate_model_judge_output_v3,
)
from app.evaluation.v3_models import EvaluationMetricV3
from tests.test_evaluation_benchmark_judging import (
    _FakeRuntime,
    _freeze_with_configuration,
    _RecordingLedger,
    _runner,
)
from tests.test_evaluation_v3_judge import MODEL, PROMPT, _answer, _bindings, _packet


def _inputs(expected: tuple[bool, ...], actual: tuple[bool, ...], *, index: int = 1):
    configuration = build_model_judge_configuration_v3(
        bindings=_bindings(successor=True).model_copy(
            update={"protocol_sha256": EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3}
        ),
        model_binding=MODEL,
        judge_prompt=PROMPT,
    )
    original = _answer(f"answer_{index:024x}")
    answer = original.model_copy(
        update={
            "citations": (
                original.citations[0].model_copy(
                    update={
                        "source_id": "source_one",
                        "source_version_id": "version_one",
                    }
                ),
            ),
            "claims": tuple(
                ClaimEvidenceV3(
                    text=f"Mệnh đề {claim_index}", citation_labels=("Nguồn 1",)
                )
                for claim_index in range(len(expected))
            ),
            "rubric_context": original.rubric_context.model_copy(
                update={
                    "evaluator_contract": "evidence_semantics_v2",
                    "required_facts": (),
                }
            ),
        }
    )
    case = build_development_calibration_case_v3(
        calibration_id=f"calibration_case_{index}", answer=answer
    )
    # Whole-answer unsupported and per-claim truth are independent labels.
    reference = build_independent_reference_labels_v3(
        case,
        development_observation_id=f"development_observation_{index}",
        claim_supported=False,
        claim_verdicts=_claim_verdicts(expected),
        adjudicator=ReferenceAdjudicatorV3(
            adjudicator_id="independent_test",
            method="automated_model",
            source_system="independently authored adversarial labels",
            model_snapshot="independent_reference_snapshot",
            instructions_sha256="a" * 64,
        ),
    )
    thresholds = build_calibration_thresholds_v3(
        minimum_development_cases=1,
        maximum_absolute_error={metric: 0.25 for metric in SEMANTIC_JUDGE_METRICS_V3},
    )
    scores = score_runtime_metrics_v3(answer)
    scores[EvaluationMetricV3.CLAIM_SUPPORT] = 0.0
    scores[EvaluationMetricV3.TASK_COMPLETION] = 0.0
    output = validate_model_judge_output_v3(
        {
            "schema_version": "3.0",
            "claim_coverage_contract": "receipt_claim_citation_coverage_v1",
            "output_schema_sha256": configuration.bindings.judge_schema_sha256,
            "opaque_answer_id": answer.opaque_answer_id,
            "verdicts": [
                {
                    "metric": metric,
                    "score": score,
                    "rubric_fact_indices": [],
                    "citation_labels": [],
                }
                for metric, score in scores.items()
            ],
            "claim_verdicts": [
                item.model_dump(mode="json") for item in _claim_verdicts(actual)
            ],
        },
        answer,
    )
    record = build_calibration_record_v3(
        configuration=configuration,
        case=case,
        reference=reference,
        thresholds=thresholds,
        output=output,
    )
    return configuration, case, reference, thresholds, record


def _claim_verdicts(values: tuple[bool, ...]):
    return tuple(
        ClaimCitationVerdictV3(
            claim_index=index,
            supported=value,
            citation_labels=("Nguồn 1",) if value else (),
            rubric_fact_indices=(),
        )
        for index, value in enumerate(values)
    )


def _freeze(inputs, *, explicit_cases=False):
    configuration, _, _, thresholds, _ = inputs[0]
    references = build_calibration_reference_bundle_v3(
        protocol_sha256=configuration.bindings.protocol_sha256,
        references=tuple(row[2] for row in inputs),
    )
    return freeze_calibration_thresholds_v3(
        configuration,
        tuple(row[4] for row in inputs),
        thresholds,
        references,
        expected_calibration_ids=tuple(row[1].calibration_id for row in inputs),
        development_cases=tuple(row[1] for row in inputs) if explicit_cases else None,
    )


def test_weighted_claim_disagreement_uses_actual_claims_and_existing_threshold():
    inputs = (
        _inputs((False,), (True,)),
        _inputs((False, False, False), (False, False, False), index=2),
    )
    freeze = _freeze(inputs)
    assert freeze.claim_calibration_claim_count == 4
    assert freeze.claim_calibration_disagreement_count == 1
    assert (
        freeze.claim_calibration_error
        == freeze.claim_calibration_error_threshold
        == 0.25
    )
    assert set(freeze.maximum_observed_errors.values()) == {0.0}


def test_claim_error_above_threshold_rejects_even_if_whole_answer_scores_match():
    with pytest.raises(ValueError, match="per-claim calibration exceeds"):
        _freeze(
            (
                _inputs((False,), (True,)),
                _inputs((False, False), (False, False), index=2),
            )
        )


def test_zero_actual_claims_cannot_freeze_calibration():
    with pytest.raises(ValueError, match="no actual development claims"):
        _freeze((_inputs((), ()),))


def test_freeze_rejects_hash_valid_record_from_foreign_development_case():
    row = _inputs((False,), (False,))
    payload = row[4].model_dump(mode="json", exclude={"record_sha256"})
    payload["development_case_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="native case|provenance drift"):
        forged = CalibrationRecordV3(**payload, record_sha256=canonical_sha256(payload))
        _freeze(((*row[:4], forged),))


@pytest.mark.parametrize("tamper", ["missing", "foreign_reference"])
def test_freeze_rejects_missing_or_foreign_record_claim_labels(tamper):
    row = _inputs((False,), (False,))
    payload = row[4].model_dump(mode="json", exclude={"record_sha256"})
    if tamper == "missing":
        for name in (
            "reference_claim_coverage",
            "observed_claim_coverage",
            "claim_disagreement_count",
            "validated_judge_output",
            "development_case",
        ):
            payload.pop(name)
    else:
        metadata = payload["reference_claim_coverage"]
        metadata["verdicts"][0]["supported"] = True
        metadata["verdicts"][0]["citation_labels"] = ["Nguồn 1"]
        metadata["supported_claim_count"] = 1
        metadata["coverage"] = 1.0
        payload["claim_disagreement_count"] = 1
    forged = CalibrationRecordV3(**payload, record_sha256=canonical_sha256(payload))
    with pytest.raises(
        ValueError, match="lacks independent per-claim|case authorities"
    ):
        _freeze(((*row[:4], forged),))


@pytest.mark.parametrize("tamper", ["opaque_answer_id", "citation_labels"])
@pytest.mark.parametrize("explicit_cases", [True, False])
def test_freeze_revalidates_saved_model_output_against_actual_native_case(
    tamper,
    explicit_cases,
):
    row = _inputs((False,), (False,))
    payload = row[4].model_dump(mode="json", exclude={"record_sha256"})
    if tamper == "opaque_answer_id":
        payload["validated_judge_output"][tamper] = "answer_000000000000000000000002"
    else:
        payload["validated_judge_output"]["claim_verdicts"][0][tamper] = ["Nguồn giả"]
        payload["observed_claim_coverage"]["verdicts"][0][tamper] = ["Nguồn giả"]
    payload["model_output_sha256"] = canonical_sha256(payload["validated_judge_output"])
    forged = CalibrationRecordV3(**payload, record_sha256=canonical_sha256(payload))
    with pytest.raises(ValueError, match="different blinded answer|unknown citation"):
        _freeze(((*row[:4], forged),), explicit_cases=explicit_cases)


def test_freeze_rejects_foreign_embedded_case_bound_to_original_reference():
    row = _inputs((False,), (False,))
    payload = row[4].model_dump(mode="json", exclude={"record_sha256"})
    foreign = build_development_calibration_case_v3(
        calibration_id=row[1].calibration_id,
        answer=row[1].answer.model_copy(update={"prompt": ("Case khác",)}),
    )
    payload["development_case"] = foreign.model_dump(mode="json")
    payload["development_case_sha256"] = foreign.case_sha256
    for key in ("reference_claim_coverage", "observed_claim_coverage"):
        payload[key]["answer_sha256"] = canonical_sha256(foreign.answer)
    forged = CalibrationRecordV3(**payload, record_sha256=canonical_sha256(payload))
    with pytest.raises(ValueError, match="provenance drift"):
        _freeze(((*row[:4], forged),))


def test_successor_persisted_calibration_revalidates_native_case_authority(tmp_path):
    row = _inputs((False,), (False,))
    configuration, case, reference, thresholds, record = row
    references = build_calibration_reference_bundle_v3(
        protocol_sha256=configuration.bindings.protocol_sha256,
        references=(reference,),
    )
    records = build_calibration_record_collection_v3(
        configuration,
        references,
        thresholds,
        (record,),
        expected_calibration_ids=(case.calibration_id,),
    )
    freeze = _freeze((row,), explicit_cases=True)
    write_calibration_artifacts_v3(
        tmp_path / "calibration",
        configuration=configuration,
        references=references,
        thresholds=thresholds,
        records=records,
        freeze=freeze,
    )
    assert (
        validate_calibration_artifacts_v3(
            tmp_path / "calibration", require_complete=True
        ).freeze
        == freeze
    )


def test_independent_reference_does_not_derive_claim_truth_from_answer_score():
    _, _, reference, _, record = _inputs((True, False), (True, False))
    assert reference.scores[EvaluationMetricV3.CLAIM_SUPPORT] == 0.0
    assert reference.claim_coverage is not None
    assert tuple(row.supported for row in reference.claim_coverage.verdicts) == (
        True,
        False,
    )
    assert record.claim_disagreement_count == 0


def test_positive_reference_conflicts_with_false_claim_label():
    _, case, reference, _, _ = _inputs((False,), (False,))
    assert reference.adjudicator is not None
    with pytest.raises(ValueError, match="positive reference support"):
        build_independent_reference_labels_v3(
            case,
            development_observation_id="development_observation_positive",
            claim_supported=True,
            claim_verdicts=_claim_verdicts((False,)),
            adjudicator=reference.adjudicator,
        )


@pytest.mark.parametrize("missing", [True, False])
def test_calibration_rejects_missing_or_same_model_reference_before_dispatch(missing):
    configuration, case, reference, thresholds, _ = _inputs((False,), (False,))
    assert reference.adjudicator is not None
    reference = build_calibration_reference_labels_v3(
        case,
        development_observation_id=reference.development_observation_id,
        source_classification=reference.source_classification,
        scores=reference.scores,
        claim_coverage=None if missing else reference.claim_coverage,
        adjudicator=reference.adjudicator
        if missing
        else reference.adjudicator.model_copy(
            update={"model_snapshot": configuration.model_binding.model}
        ),
    )
    called = []
    with pytest.raises(
        ValueError, match="independent per-claim|differ from the scored judge"
    ):
        run_development_calibration_case_v3(
            configuration,
            case,
            reference=reference,
            thresholds=thresholds,
            judge=lambda request: called.append(request),
        )
    assert called == []


@pytest.mark.parametrize("entrypoint", ["direct", "ledger", "replay"])
def test_successor_heldout_rejects_legacy_freeze_at_every_judge_entrypoint(entrypoint):
    row = _inputs((False,), (False,))
    configuration, case, _, _, _ = row
    freeze = _freeze((row,))
    payload = {
        key: value
        for key, value in freeze.model_dump(
            mode="json", exclude={"calibration_sha256"}
        ).items()
        if not key.startswith("claim_calibration_")
    }
    legacy = CalibrationFreezeV3(
        **payload, calibration_sha256=canonical_sha256(payload)
    )
    packet = _packet(configuration.bindings, (case.answer,))
    if entrypoint == "direct":
        called = []
        with pytest.raises(ValueError, match="per-claim calibration freeze"):
            judge_blinded_packet_v3(
                packet,
                configuration,
                legacy,
                judge=lambda request: called.append(request),
            )

        assert called == []
    elif entrypoint == "ledger":
        with pytest.raises(
            FrozenJudgeRunError, match="calibration_claim_coverage_missing"
        ):
            build_heldout_judge_run_key_v3(
                packet=packet,
                configuration=configuration,
                calibration=legacy,
                additive_source_manifest_sha256="a" * 64,
            )
    else:
        with pytest.raises(
            FrozenJudgeRunError, match="calibration_claim_coverage_missing"
        ):
            _terminal_result(
                terminal={"status": "completed", "ledger_attempts": [], "artifact": {}},
                identity=JudgeJobIdentityV3(
                    phase="held_out_scoring",
                    target_id=case.answer.opaque_answer_id,
                    source_sha256=canonical_sha256(case.answer),
                    run_key_sha256="a" * 64,
                ),
                expected_phase="held_out_scoring",
                packet=packet,
                configuration=configuration,
                calibration=legacy,
                answer=case.answer,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["packet", "answer", "development"])
async def test_ledger_rejects_uncalibrated_claim_inputs_before_any_scope_or_dispatch(
    entrypoint,
    tmp_path,
):
    row = _inputs((False,), (False,))
    configuration, case, reference, thresholds, _ = row
    ledger = _RecordingLedger()
    runtime = _FakeRuntime(ledger)
    runner = _runner(runtime, ledger, tmp_path / "rejected.jsonl")
    if entrypoint == "development":
        reference = build_calibration_reference_labels_v3(
            case,
            development_observation_id=reference.development_observation_id,
            source_classification=reference.source_classification,
            scores=reference.scores,
            adjudicator=reference.adjudicator,
        )
        bundle = build_calibration_reference_bundle_v3(
            protocol_sha256=configuration.bindings.protocol_sha256,
            references=(reference,),
        )
        with pytest.raises(ValueError, match="independent per-claim"):
            await runner.run_development_calibration_cases(
                configuration=configuration,
                cases=(case,),
                references=bundle,
                thresholds=thresholds,
            )
    else:
        freeze = _freeze((row,))
        payload = {
            key: value
            for key, value in freeze.model_dump(
                mode="json", exclude={"calibration_sha256"}
            ).items()
            if not key.startswith("claim_calibration_")
        }
        legacy = CalibrationFreezeV3(
            **payload, calibration_sha256=canonical_sha256(payload)
        )
        kwargs = dict(
            packet=_packet(configuration.bindings, (case.answer,)),
            configuration=configuration,
            calibration=legacy,
        )
        with pytest.raises(
            FrozenJudgeRunError, match="calibration_claim_coverage_missing"
        ):
            if entrypoint == "packet":
                await runner.run_heldout_packet(**kwargs)
            else:
                await runner.run_heldout_answer(
                    **kwargs, opaque_answer_id=case.answer.opaque_answer_id
                )
    assert runtime.requests == []
    assert ledger.created_scopes == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("successor", [True, False])
@pytest.mark.parametrize("mixed", [True, False])
@pytest.mark.parametrize("entrypoint", ["direct", "key", "packet", "answer"])
async def test_packet_schema_preflight_rejects_all_contract_mismatches_without_dispatch(
    successor,
    mixed,
    entrypoint,
    tmp_path,
):
    row = _inputs((False,), (False,))
    if successor:
        configuration, case = row[:2]
        valid = case.answer
        wrong = valid.model_copy(
            update={
                "opaque_answer_id": "answer_000000000000000000000002",
                "rubric_context": valid.rubric_context.model_copy(
                    update={"evaluator_contract": None}
                ),
            }
        )
        freeze = _freeze((row,))
    else:
        configuration = build_model_judge_configuration_v3(
            bindings=_bindings().model_copy(
                update={"protocol_sha256": EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3}
            ),
            model_binding=MODEL,
            judge_prompt=PROMPT,
        )
        valid = _answer("answer_000000000000000000000001")
        wrong = row[1].answer.model_copy(
            update={"opaque_answer_id": "answer_000000000000000000000002"}
        )
        freeze = _freeze_with_configuration(configuration.configuration_sha256)
    packet = _packet(configuration.bindings, (valid, wrong) if mixed else (wrong,))
    if entrypoint == "direct":
        called = []
        with pytest.raises(ValueError, match="contract differs from judge schema"):
            judge_blinded_packet_v3(
                packet,
                configuration,
                freeze,
                judge=lambda request: called.append(request),
            )
        assert called == []
        return
    if entrypoint == "key":
        with pytest.raises(
            FrozenJudgeRunError, match="blind_answer_judge_schema_mismatch"
        ):
            build_heldout_judge_run_key_v3(
                packet=packet,
                configuration=configuration,
                calibration=freeze,
                additive_source_manifest_sha256="a" * 64,
            )
        return
    ledger = _RecordingLedger()
    runtime = _FakeRuntime(ledger)
    runner = _runner(runtime, ledger, tmp_path / "mismatched-packet.jsonl")
    with pytest.raises(FrozenJudgeRunError, match="blind_answer_judge_schema_mismatch"):
        if entrypoint == "packet":
            await runner.run_heldout_packet(
                packet=packet, configuration=configuration, calibration=freeze
            )
        else:
            # Selecting a valid first answer cannot bypass a later mismatched answer.
            await runner.run_heldout_answer(
                packet=packet,
                configuration=configuration,
                calibration=freeze,
                opaque_answer_id=packet.answers[0].opaque_answer_id,
            )
    assert runtime.requests == []
    assert ledger.created_scopes == {}


def test_hash_valid_calibration_replay_rejects_foreign_saved_output():
    configuration, case, reference, thresholds, record = _inputs((False,), (False,))
    payload: dict[str, Any] = record.model_dump(mode="json", exclude={"record_sha256"})
    payload["validated_judge_output"]["opaque_answer_id"] = (
        "answer_000000000000000000000002"
    )
    payload["model_output_sha256"] = canonical_sha256(payload["validated_judge_output"])
    forged = CalibrationRecordV3(**payload, record_sha256=canonical_sha256(payload))
    with pytest.raises(
        FrozenJudgeRunError, match="calibration_journal_claim_coverage_mismatch"
    ):
        _terminal_result(
            terminal={
                "status": "completed",
                "ledger_attempts": [],
                "artifact": forged.model_dump(mode="json"),
            },
            identity=JudgeJobIdentityV3(
                phase="development_calibration",
                target_id=case.calibration_id,
                source_sha256=case.case_sha256,
                run_key_sha256="a" * 64,
            ),
            expected_phase="development_calibration",
            configuration=configuration,
            case=case,
            reference=reference,
            thresholds=thresholds,
        )
