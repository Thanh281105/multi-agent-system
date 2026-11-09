"""Chấm lại (test-retest) 720 câu trả lời của gói P8 bằng cùng cấu hình judge.

Chỉ đo độ ồn của judge, không đo độ đúng. Đọc artifact frozen, không ghi đè.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.evaluation.benchmark_judging import _serialize_model_judge_input
from app.evaluation.protocol import canonical_sha256
from app.evaluation.rejudge_collect import JudgeRequest
from app.evaluation.rejudge_stats import (
    PairedDelta,
    agreement,
    cohens_kappa,
    paired_variant_delta,
)
from app.evaluation.v3_artifacts import BlindedAnswerPacketV3, UnblindingKeyV3
from app.evaluation.v3_judge import (
    CalibrationFreezeV3,
    ModelJudgeConfigurationV3,
    ModelJudgeRequestV3,
    judge_blinded_packet_v3,
    model_judge_output_type_v3,
)

PREPARATION_DIR = "p8-preparation"
SCORED_DIR = "p8-publication/scored-artifacts"
MODEL_JUDGE_METRICS = (
    "task_completion",
    "answerability_abstention",
    "claim_support",
    "useful_continuation",
    "valid_plan",
)
SA = "sa_shared_tools_rag"
MA_ADAPTIVE = "ma_adaptive_rag"


@dataclass(frozen=True)
class P8Artifacts:
    packet: BlindedAnswerPacketV3
    unblinding: UnblindingKeyV3
    configuration: ModelJudgeConfigurationV3
    calibration: CalibrationFreezeV3
    original_scores: dict[str, dict[str, float | None]]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_p8_artifacts(p8_root: Path) -> P8Artifacts:
    prep = p8_root / PREPARATION_DIR
    scored = p8_root / SCORED_DIR
    judgments = _load_json(scored / "judgments.json")["judgments"]
    return P8Artifacts(
        packet=BlindedAnswerPacketV3.model_validate(
            _load_json(scored / "blinded_answers.json")
        ),
        unblinding=UnblindingKeyV3.model_validate(
            _load_json(scored / "unblinding_key.json")
        ),
        configuration=ModelJudgeConfigurationV3.model_validate(
            _load_json(prep / "judge-configuration.p8.json")
        ),
        calibration=CalibrationFreezeV3.model_validate(
            _load_json(prep / "calibration-freeze.p8.json")
        ),
        original_scores={j["opaque_answer_id"]: j["scores"] for j in judgments},
    )


def build_requests(
    artifacts: P8Artifacts, *, sample: Sequence[str] | None = None
) -> dict[str, JudgeRequest]:
    configuration = artifacts.configuration
    schema = model_judge_output_type_v3(configuration.bindings.judge_schema_sha256)
    wanted = set(sample) if sample is not None else None
    requests: dict[str, JudgeRequest] = {}
    for answer in artifacts.packet.answers:
        if wanted is not None and answer.opaque_answer_id not in wanted:
            continue
        request = ModelJudgeRequestV3(
            phase="held_out_scoring",
            judge_prompt=configuration.judge_prompt,
            output_schema_sha256=configuration.bindings.judge_schema_sha256,
            configuration_sha256=configuration.configuration_sha256,
            calibration_sha256=artifacts.calibration.calibration_sha256,
            answer=answer,
        )
        requests[answer.opaque_answer_id] = JudgeRequest(
            instructions=configuration.judge_prompt,
            input_text=_serialize_model_judge_input(request),
            schema=schema,
            max_output_tokens=(
                3_600 if answer.rubric_context.evaluator_contract is not None else 1_200
            ),
        )
    return requests


def _constant_judge(output: Any) -> Callable[[Any], Any]:
    return lambda _request: output


def _single_answer_packet(
    packet: BlindedAnswerPacketV3, opaque_answer_id: str
) -> BlindedAnswerPacketV3:
    answer = next(a for a in packet.answers if a.opaque_answer_id == opaque_answer_id)
    payload = packet.model_dump(mode="json", exclude={"packet_sha256"})
    payload["answers"] = [answer.model_dump(mode="json")]
    payload["answer_count"] = 1
    payload["packet_sha256"] = canonical_sha256(payload)
    return BlindedAnswerPacketV3.model_validate(payload)


def rejudge_scores(
    artifacts: P8Artifacts, cache_rows: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, dict[str, float | None]], dict[str, str]]:
    """Chấm lại offline từ cache; mỗi câu lỗi được ghi mã lỗi, không làm sập cả lượt."""
    schema = model_judge_output_type_v3(
        artifacts.configuration.bindings.judge_schema_sha256
    )
    scores: dict[str, dict[str, float | None]] = {}
    errors: dict[str, str] = {}
    for key, row in cache_rows.items():
        if row.get("error") is not None or row.get("output") is None:
            errors[key] = str(row.get("error") or "empty_output")
            continue
        try:
            output = schema.model_validate(row["output"])
            records = judge_blinded_packet_v3(
                _single_answer_packet(artifacts.packet, key),
                artifacts.configuration,
                artifacts.calibration,
                judge=_constant_judge(output),
            )
        except ValueError as exc:  # pydantic.ValidationError là ValueError
            errors[key] = f"invalid_output: {str(exc)[:120]}"
            continue
        scores[key] = {m.value: s for m, s in records[0].scores.items()}
    return scores, errors


def variant_group_scores(
    artifacts: P8Artifacts,
    scores_by_answer: Mapping[str, Mapping[str, float | None]],
    metric: str,
) -> dict[tuple[str, str], float]:
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for entry in artifacts.unblinding.entries:
        value = scores_by_answer.get(entry.opaque_answer_id, {}).get(metric)
        if value is not None:
            buckets[(entry.work_group_id, str(entry.variant_id))].append(value)
    return {key: sum(v) / len(v) for key, v in buckets.items()}


def compare_runs(
    artifacts: P8Artifacts,
    new_scores: Mapping[str, Mapping[str, float | None]],
    *,
    resamples: int = 10_000,
) -> dict[str, Any]:
    common = sorted(set(new_scores) & set(artifacts.original_scores))
    per_metric: dict[str, Any] = {}
    for metric in MODEL_JUDGE_METRICS:
        pairs = [
            (artifacts.original_scores[k].get(metric), new_scores[k].get(metric))
            for k in common
        ]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        first = [a for a, _ in pairs]
        second = [b for _, b in pairs]
        per_metric[metric] = {
            "n": len(pairs),
            "agreement": agreement(first, second),
            "kappa": cohens_kappa(first, second),
        }
    deltas: dict[str, dict[str, PairedDelta]] = {}
    for label, source in (
        ("original", artifacts.original_scores),
        ("rejudge", new_scores),
    ):
        restricted = {k: source[k] for k in common}
        scores = variant_group_scores(artifacts, restricted, "task_completion")
        deltas[label] = {
            "sa_vs_ma_adaptive": paired_variant_delta(
                scores, baseline=SA, treatment=MA_ADAPTIVE, resamples=resamples
            )
        }
    return {"answers_compared": len(common), "metrics": per_metric, "deltas": deltas}
