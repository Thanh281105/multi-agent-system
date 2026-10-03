"""Explicit successor protocol authority for durable offline judge jobs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.evaluation.benchmark_judging import (
    DurableModelJudgeRunnerV3,
    FrozenJudgeRunError,
    JudgeBudgetPolicyV3,
    build_development_judge_run_key_v3,
    build_heldout_judge_run_key_v3,
)
from app.evaluation.benchmark_v3 import SuccessorBindingV3
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_judge import (
    build_calibration_record_v3,
    build_calibration_reference_bundle_v3,
    build_model_judge_configuration_v3,
    validate_model_judge_output_v3,
)
from app.shared.budget import current_provider_budget
from tests.test_evaluation_benchmark_judging import _RecordingLedger
from tests.test_evaluation_v3_claim_calibration import _freeze, _inputs
from tests.test_evaluation_v3_judge import MODEL, PROMPT, _bindings, _packet


def _successor_inputs():
    original, case, reference, thresholds, record = _inputs((False,), (False,))
    configuration = build_model_judge_configuration_v3(
        bindings=original.bindings.model_copy(update={"protocol_sha256": "f" * 64}),
        model_binding=MODEL,
        judge_prompt=PROMPT,
    )
    record = build_calibration_record_v3(
        configuration=configuration,
        case=case,
        reference=reference,
        thresholds=thresholds,
        output=validate_model_judge_output_v3(
            record.validated_judge_output, case.answer
        ),
    )
    row = configuration, case, reference, thresholds, record
    bundle = build_calibration_reference_bundle_v3(
        protocol_sha256=configuration.bindings.protocol_sha256,
        references=(reference,),
    )
    payload = {
        "schema_version": "3.1",
        "binding_id": "successor_judge_test",
        "evaluator_contract": "evidence_semantics_v2",
        "protocol_sha256": configuration.bindings.protocol_sha256,
        "repeat_decision_sha256": configuration.bindings.repeat_decision_sha256,
        "gold_sha256": configuration.bindings.gold_sha256,
        "split_sha256": configuration.bindings.split_sha256,
        "experiment_path": "evaluation/successor/experiment.json",
        "gold_path": "evaluation/successor/gold.json",
        "split_path": "evaluation/successor/split.json",
        "runtime_manifest_path": "evaluation/successor/runtime.json",
        "runtime_manifest_sha256": "9" * 64,
    }
    binding = SuccessorBindingV3(**payload, binding_sha256=canonical_sha256(payload))
    return row, bundle, _freeze((row,)), binding


def _key(phase, row, references, freeze, binding=None):
    configuration, case, _, thresholds, _ = row
    common = dict(
        configuration=configuration,
        additive_source_manifest_sha256="a" * 64,
        successor_binding=binding,
    )
    if phase == "development":
        return build_development_judge_run_key_v3(
            references=references, thresholds=thresholds, **common
        )
    return build_heldout_judge_run_key_v3(
        packet=_packet(configuration.bindings, (case.answer,)),
        calibration=freeze,
        **common,
    )


@pytest.mark.parametrize("phase", ["development", "heldout"])
def test_new_protocol_requires_explicit_validated_successor_authority(phase):
    row, references, freeze, binding = _successor_inputs()
    with pytest.raises(FrozenJudgeRunError, match="package7_protocol_hash_drift"):
        _key(phase, row, references, freeze)
    key = _key(phase, row, references, freeze, binding)
    assert key.protocol_sha256 == binding.protocol_sha256
    assert key.payload()["successor_binding_sha256"] == binding.binding_sha256


@pytest.mark.parametrize(
    "field",
    ["protocol_sha256", "repeat_decision_sha256", "gold_sha256", "split_sha256"],
)
@pytest.mark.parametrize("phase", ["development", "heldout"])
def test_successor_authority_rejects_rehashed_pin_mismatch(phase, field):
    row, references, freeze, binding = _successor_inputs()
    payload = binding.model_dump(mode="json", exclude={"binding_sha256"})
    payload[field] = "e" * 64
    wrong = SuccessorBindingV3(**payload, binding_sha256=canonical_sha256(payload))
    with pytest.raises(FrozenJudgeRunError, match="successor_judge_bindings_mismatch"):
        _key(phase, row, references, freeze, wrong)


def test_successor_authority_rejects_noncanonical_binding_and_legacy_schema():
    row, references, freeze, binding = _successor_inputs()
    with pytest.raises(ValueError, match="canonical hash mismatch"):
        _key(
            "development",
            row,
            references,
            freeze,
            binding.model_copy(update={"binding_sha256": "a" * 64}),
        )
    legacy_configuration = build_model_judge_configuration_v3(
        bindings=_bindings().model_copy(
            update={"protocol_sha256": binding.protocol_sha256}
        ),
        model_binding=MODEL,
        judge_prompt=PROMPT,
    )
    with pytest.raises(FrozenJudgeRunError, match="successor_judge_schema_mismatch"):
        _key(
            "development", (legacy_configuration, *row[1:]), references, freeze, binding
        )


def test_default_historical_key_payload_remains_unchanged():
    row = _inputs((False,), (False,))
    references = build_calibration_reference_bundle_v3(
        protocol_sha256=row[0].bindings.protocol_sha256,
        references=(row[2],),
    )
    key = _key("development", row, references, _freeze((row,)))
    expected = {
        "protocol_sha256": row[0].bindings.protocol_sha256,
        "configuration_sha256": row[0].configuration_sha256,
        "calibration_sha256": None,
        "blinded_packet_sha256": None,
        "development_inputs_sha256": canonical_sha256(
            {
                "reference_bundle_sha256": references.reference_bundle_sha256,
                "thresholds_sha256": row[3].thresholds_sha256,
            }
        ),
        "additive_source_manifest_sha256": "a" * 64,
    }
    assert key.payload() == expected
    assert key.run_key_sha256 == canonical_sha256(expected)


@pytest.mark.asyncio
async def test_successor_runner_completes_and_replays_both_phases_without_real_egress(
    tmp_path,
):
    row, references, freeze, binding = _successor_inputs()
    configuration, case, _, thresholds, record = row
    ledger = _RecordingLedger()

    class Runtime:
        requests = []

        async def generate_structured(self, **kwargs):
            budget = current_provider_budget()
            assert budget is not None and budget.ledger is ledger
            assert kwargs["schema"].__name__ == "ClaimCoverageModelJudgeOutputV3"
            self.requests.append(json.loads(kwargs["input_text"]))
            ledger.reserve(budget.scope_id)
            return SimpleNamespace(value=record.validated_judge_output)

    runtime = Runtime()

    def runner(name):
        return DurableModelJudgeRunnerV3(
            runtime=runtime,
            ledger=ledger,
            account_id="judge-account",
            journal_path=tmp_path / name,
            additive_source_manifest_sha256="a" * 64,
            budget_policy=JudgeBudgetPolicyV3(hard_limit_nano_usd=1000),
            successor_binding=binding,
        )

    development = runner("development.jsonl")
    arguments = dict(
        configuration=configuration,
        cases=(case,),
        references=references,
        thresholds=thresholds,
    )
    first = await development.run_development_calibration_cases(**arguments)
    assert first[0].status == "completed" and first[0].record == record
    assert (await development.run_development_calibration_cases(**arguments))[
        0
    ].replayed
    heldout = runner("heldout.jsonl")
    arguments = dict(
        configuration=configuration,
        packet=_packet(configuration.bindings, (case.answer,)),
        calibration=freeze,
    )
    assert (await heldout.run_heldout_packet(**arguments))[0].status == "completed"
    assert (await heldout.run_heldout_packet(**arguments))[0].replayed
    assert len(runtime.requests) == len(ledger.created_scopes) == 2
