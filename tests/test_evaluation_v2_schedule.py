"""Paired/interleaved evaluation v2 schedule tests."""

from __future__ import annotations

from collections import defaultdict

from app.evaluation.schedule import build_evaluation_schedule_v2
from app.evaluation.v2_models import (
    EvaluationPhase,
    EvaluationProtocolV2,
    EvaluationVariantV2,
    RuntimeMode,
)


def test_schedule_is_reproducible_paired_and_discards_warmup_orders() -> None:
    protocol = _protocol()

    first = build_evaluation_schedule_v2(protocol)
    second = build_evaluation_schedule_v2(protocol)

    assert first == second
    warmups = [turn for turn in first if turn.is_warmup]
    measurements = [turn for turn in first if not turn.is_warmup]
    assert len(warmups) == 2
    assert all(turn.phase is None and turn.execution_order is None for turn in warmups)
    assert {turn.case_id for turn in warmups} == {"case_a"}
    assert len(measurements) == 10
    assert [turn.execution_order for turn in measurements] == list(range(10))

    blocks: dict[tuple[EvaluationPhase | None, str, int], set[str]] = defaultdict(set)
    for turn in measurements:
        blocks[(turn.phase, turn.case_id, turn.repetition)].add(turn.variant_id)
    assert all(
        variants == {"baseline_v2", "candidate_v2"} for variants in blocks.values()
    )
    assert set(blocks) == {
        (EvaluationPhase.CORRECTNESS, "case_a", 0),
        (EvaluationPhase.CORRECTNESS, "case_a", 1),
        (EvaluationPhase.CORRECTNESS, "case_b", 0),
        (EvaluationPhase.CORRECTNESS, "case_b", 1),
        (EvaluationPhase.LATENCY, "case_b", 0),
    }


def test_measurement_schedule_does_not_change_when_warmups_change() -> None:
    with_warmup = build_evaluation_schedule_v2(_protocol())
    without_warmup = build_evaluation_schedule_v2(
        _protocol().model_copy(update={"warmup_repeats": 0, "warmup_case_id": None})
    )

    assert tuple(turn for turn in with_warmup if not turn.is_warmup) == without_warmup


def _protocol() -> EvaluationProtocolV2:
    variants = (
        EvaluationVariantV2(
            variant_id="baseline_v2",
            description="Deterministic baseline.",
            runtime_mode=RuntimeMode.DETERMINISTIC,
            enabled_agents=("product_agent",),
            embedding_backend="hashing",
        ),
        EvaluationVariantV2(
            variant_id="candidate_v2",
            description="Second deterministic schedule peer.",
            runtime_mode=RuntimeMode.DETERMINISTIC,
            enabled_agents=("product_agent",),
            embedding_backend="hashing",
            parent_variant_id="baseline_v2",
        ),
    )
    return EvaluationProtocolV2(
        protocol_id="schedule_protocol_v2",
        experiment_sha256="a" * 64,
        baseline_variant_id="baseline_v2",
        corpus_id="schedule_corpus_v2",
        corpus_sha256="b" * 64,
        dataset_id="schedule_dataset_v2",
        dataset_sha256="c" * 64,
        evaluator_sha256="d" * 64,
        random_seed=73,
        correctness_repeats=2,
        warmup_repeats=1,
        latency_repeats=1,
        case_order=("case_a", "case_b"),
        latency_case_order=("case_b",),
        warmup_case_id="case_a",
        variants=variants,
        git_revision="abcdef1",
        git_dirty=False,
    )
