"""Describe submitted blind ratings, never synthesize human judgments."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.evaluation.protocol import canonical_json_bytes
from app.evaluation.v3_artifacts import BlindedAnswerPacketV3
from app.evaluation.v3_models import EvaluationMetricV3


class BlindRating(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opaque_answer_id: str
    metric: EvaluationMetricV3
    reviewer_a_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    reviewer_b_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    adjudicator_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    human_a: StrictInt = Field(ge=0, le=1)
    human_b: StrictInt = Field(ge=0, le=1)
    judge: StrictInt = Field(ge=0, le=1)
    adjudicated: StrictInt = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_reviewers(self) -> BlindRating:
        if len({self.reviewer_a_id, self.reviewer_b_id, self.adjudicator_id}) != 3:
            raise ValueError("reviewers and adjudicator must be distinct")
        return self


def agreement(first: list[int], second: list[int]) -> dict[str, Any]:
    if not first or len(first) != len(second):
        raise ValueError("agreement requires nonempty paired labels")
    count = len(first)
    agreed = sum(a == b for a, b in zip(first, second, strict=True))
    p_first = sum(first) / count
    p_second = sum(second) / count
    expected = p_first * p_second + (1 - p_first) * (1 - p_second)
    return {
        "count": count,
        "agreed": agreed,
        "raw_agreement_rate": agreed / count,
        "cohens_kappa": (
            (agreed / count - expected) / (1 - expected) if expected < 1 else None
        ),
    }


def summarize_ratings(packet_path: Path, ratings_path: Path) -> dict[str, Any]:
    packet_bytes = packet_path.read_bytes()
    packet = BlindedAnswerPacketV3.model_validate_json(packet_bytes)
    ratings_bytes = ratings_path.read_bytes()
    rows = [
        BlindRating.model_validate_json(line)
        for line in ratings_bytes.decode("utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError("ratings must contain at least one submitted judgment")
    known = {answer.opaque_answer_id for answer in packet.answers}
    seen: set[tuple[str, EvaluationMetricV3]] = set()
    groups: dict[EvaluationMetricV3, list[BlindRating]] = defaultdict(list)
    for row in rows:
        key = (row.opaque_answer_id, row.metric)
        if row.opaque_answer_id not in known:
            raise ValueError("rating references an answer outside the blind packet")
        if key in seen:
            raise ValueError("duplicate answer/metric rating")
        seen.add(key)
        groups[row.metric].append(row)
    metrics: dict[str, Any] = {}
    for metric, group in sorted(groups.items()):
        human = agreement([r.human_a for r in group], [r.human_b for r in group])
        judge = agreement([r.judge for r in group], [r.adjudicated for r in group])
        confusion = {
            "true_positives": sum(r.judge == 1 and r.adjudicated == 1 for r in group),
            "true_negatives": sum(r.judge == 0 and r.adjudicated == 0 for r in group),
            "false_positives": sum(r.judge == 1 and r.adjudicated == 0 for r in group),
            "false_negatives": sum(r.judge == 0 and r.adjudicated == 1 for r in group),
        }
        metrics[metric.value] = {
            "human_agreement": human,
            "judge_vs_adjudicated": judge,
            "confusion_matrix": confusion,
        }
    return {
        "schema_version": "submitted_blind_ratings_summary_v1",
        "status": "descriptive_only_not_calibration_freeze",
        "collection_authority": "submitted_labels_not_independently_attested",
        "packet_sha256": packet.packet_sha256,
        "packet_file_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "ratings_file_sha256": hashlib.sha256(ratings_bytes).hexdigest(),
        "sample_size": len({r.opaque_answer_id for r in rows}),
        "rating_count": len(rows),
        "packet_answer_count": packet.answer_count,
        "metrics": metrics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--ratings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        report = summarize_ratings(arguments.packet, arguments.ratings)
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        with arguments.output.open("xb") as handle:
            handle.write(canonical_json_bytes(report))
    except (OSError, ValueError):
        parser.exit(
            1,
            "Calibration summary refused: invalid/unreadable packet or ratings, "
            "or an existing output. Source contents are not logged.\n",
        )
    print(
        json.dumps({"status": report["status"], "rating_count": report["rating_count"]})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
