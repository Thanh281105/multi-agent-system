import pytest

from app.evaluation.rejudge_stats import agreement, cohens_kappa, paired_variant_delta


def test_agreement_basic() -> None:
    assert agreement([1, 0, 1, 1], [1, 0, 0, 1]) == 0.75


def test_agreement_empty_returns_none() -> None:
    assert agreement([], []) is None


def test_kappa_known_value() -> None:
    a = [1] * 3 + [1] * 2 + [0] * 1 + [0] * 4
    b = [1] * 3 + [0] * 2 + [1] * 1 + [0] * 4
    assert cohens_kappa(a, b) == pytest.approx(0.4)


def test_kappa_degenerate_returns_none() -> None:
    assert cohens_kappa([1, 1, 1], [1, 1, 1]) is None
    assert cohens_kappa([], []) is None


def _scores() -> dict[tuple[str, str], float]:
    return {
        ("g1", "sa"): 0.5,
        ("g1", "ma"): 0.7,
        ("g2", "sa"): 0.5,
        ("g2", "ma"): 0.5,
        ("g3", "sa"): 0.5,
        ("g3", "ma"): 0.4,
    }


def test_paired_delta_wtl_and_mean() -> None:
    result = paired_variant_delta(
        _scores(), baseline="sa", treatment="ma", resamples=500
    )
    assert result.mean_delta == pytest.approx(0.1 / 3)
    assert (result.wins, result.ties, result.losses) == (1, 1, 1)
    assert result.ci_low <= result.mean_delta <= result.ci_high


def test_paired_delta_deterministic_seed() -> None:
    a = paired_variant_delta(_scores(), baseline="sa", treatment="ma", resamples=500)
    b = paired_variant_delta(_scores(), baseline="sa", treatment="ma", resamples=500)
    assert a == b
