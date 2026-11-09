from pathlib import Path

import pytest

from app.evaluation.rejudge_p8 import (
    MA_ADAPTIVE,
    SA,
    P8Artifacts,
    build_requests,
    compare_runs,
    load_p8_artifacts,
    rejudge_scores,
    variant_group_scores,
)

P8_ROOT = (
    Path(__file__).resolve().parents[1]
    / "output"
    / "evaluation-v3"
    / "heldout-mini-luna-judge-v3-rating-fix-20261014"
)
needs_p8 = pytest.mark.skipif(
    not (P8_ROOT / "p8-publication" / "scored-artifacts" / "judgments.json").exists(),
    reason="gói P8 gốc nằm trong output/ (gitignored) và không có trên máy này",
)


@pytest.fixture(scope="module")
def p8() -> P8Artifacts:
    return load_p8_artifacts(P8_ROOT)


@needs_p8
def test_build_requests_keys_are_opaque_ids(p8: P8Artifacts) -> None:
    requests = build_requests(p8)
    assert len(requests) == 720
    assert set(requests) == {a.opaque_answer_id for a in p8.packet.answers}


@needs_p8
def test_build_requests_sample_subset(p8: P8Artifacts) -> None:
    ids = [p8.packet.answers[0].opaque_answer_id, p8.packet.answers[1].opaque_answer_id]
    assert set(build_requests(p8, sample=ids)) == set(ids)


@needs_p8
def test_rejudge_from_original_outputs_reproduces_original_scores(
    p8: P8Artifacts,
) -> None:
    """Dùng phán quyết gốc làm cache: pipeline offline phải tái tạo đúng điểm gốc."""
    import json

    judgments = json.loads(
        (P8_ROOT / "p8-publication/scored-artifacts/judgments.json").read_text(
            encoding="utf-8"
        )
    )["judgments"]
    rows = {
        j["opaque_answer_id"]: {"output": j["validated_judge_output"], "error": None}
        for j in judgments[:6]
    }
    scores, errors = rejudge_scores(p8, rows)
    assert errors == {}
    for key, got in scores.items():
        assert got == p8.original_scores[key]


@needs_p8
def test_missing_or_error_rows_are_reported_not_raised(p8: P8Artifacts) -> None:
    some = p8.packet.answers[0].opaque_answer_id
    other = p8.packet.answers[1].opaque_answer_id
    scores, errors = rejudge_scores(
        p8,
        {
            some: {"output": None, "error": "timeout"},
            other: {"output": {"bad": 1}, "error": None},
        },
    )
    assert scores == {}
    assert errors[some] == "timeout"
    assert errors[other].startswith("invalid_output")


@needs_p8
def test_regression_original_delta_matches_analysis(p8: P8Artifacts) -> None:
    """Hồi quy: tái tạo mean_delta task_completion SA -> MA adaptive (gốc)."""
    result = compare_runs(p8, p8.original_scores, resamples=200)
    delta = result["deltas"]["original"]["sa_vs_ma_adaptive"]
    assert delta.mean_delta == pytest.approx(-0.038065843621399184)
    assert (delta.wins, delta.ties, delta.losses) == (2, 18, 7)
    # so chính nó với chính nó: đồng thuận tuyệt đối
    for metric in result["metrics"].values():
        assert metric["agreement"] == 1.0


@needs_p8
def test_variant_group_scores_covers_groups(p8: P8Artifacts) -> None:
    scores = variant_group_scores(p8, p8.original_scores, "task_completion")
    assert {v for _, v in scores} >= {SA, MA_ADAPTIVE}


@needs_p8
def test_compare_runs_small_sample_without_common_groups(p8: P8Artifacts) -> None:
    """Mẫu nhỏ không có work group chung SA/MA: không sập, delta là None."""
    first = p8.unblinding.entries[0].opaque_answer_id
    scores = {first: p8.original_scores[first]}
    result = compare_runs(p8, scores, resamples=50)
    assert result["answers_compared"] == 1
    assert result["deltas"]["rejudge"]["sa_vs_ma_adaptive"] is None
