"""Server-owned model fact catalog tests."""

from __future__ import annotations

import pytest

from app.shared.model_data import model_fact_catalog


def test_fact_catalog_is_stable_bounded_and_redacted() -> None:
    payload = {
        "zeta": 2,
        "api_key": "provider-secret-canary",
        "alpha": {"reviews": ["one", "two"], "rating": 4.8},
    }

    catalog = model_fact_catalog(
        payload,
        source_ids=("postgresql:products",),
        max_facts=2,
    )

    assert [item["fact_id"] for item in catalog] == ["fact_001", "fact_002"]
    assert [item["path"] for item in catalog] == [
        "data.alpha.rating",
        "data.alpha.review_count",
    ]
    assert all(item["source_ids"] == ["postgresql:products"] for item in catalog)
    assert "provider-secret-canary" not in repr(catalog)


@pytest.mark.parametrize("max_facts", [0, 101])
def test_fact_catalog_rejects_unsafe_bounds(max_facts: int) -> None:
    with pytest.raises(ValueError, match="max_facts"):
        model_fact_catalog({}, source_ids=(), max_facts=max_facts)
