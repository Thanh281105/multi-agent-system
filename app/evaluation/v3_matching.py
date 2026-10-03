"""Exact predicates for the versioned evidence_semantics_v2 rubric.

Semantic entailment stays at the calibrated model or human boundary; these
helpers never approximate it with English substring matching.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from app.evaluation.v3_artifacts import CitationForReviewV3, RubricFactV3

_NUMBER = re.compile(r"(?<![\w.,/])[-+]?\d+(?:[.,]\d+)?(?![\w.,/])")
_CATALOG_FIELDS = {
    "/name": "title",
    "/price_vnd": "snapshot_price_vnd",
    "/rating": "snapshot_rating",
    "/source_review_count": "snapshot_review_count",
    "/publisher": "publisher",
    "/page_count": "page_count",
    "/category": "category",
}


def normalized_value_v3(value: object) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )
    return " ".join(text.casefold().split())


def exact_value_in_text_v3(value: object, text: str, *, numeric: bool) -> bool:
    if numeric:
        if isinstance(value, bool) or value is None:
            return False
        try:
            expected = Decimal(str(value))
        except InvalidOperation:
            return False
        return expected.is_finite() and any(
            Decimal(match.group().replace(",", ".")) == expected
            for match in _NUMBER.finditer(text)
        )
    expected_text = normalized_value_v3(value)
    # Complete token boundaries prevent "Sapiens" matching "SapiensX".
    return (
        bool(expected_text)
        and re.search(
            rf"(?<!\w){re.escape(expected_text)}(?!\w)", normalized_value_v3(text)
        )
        is not None
    )


def citation_supports_fact_v3(
    citation: CitationForReviewV3, fact: RubricFactV3
) -> bool:
    """Match the immutable record and field, independently of presentation text."""
    if fact.support_kind == "source_excerpt":
        return (
            citation.source_id == fact.support_record_id
            and bool(citation.source_version_id)
            and normalized_value_v3(fact.evidence)
            in normalized_value_v3(citation.evidence)
        )
    if (
        citation.source_id != f"catalog_{fact.support_record_id}"
        or not citation.source_version_id
    ):
        return False
    field = _CATALOG_FIELDS.get(fact.support_json_pointer or "")
    if field is None:
        return False
    values = [
        line.partition(":")[2].strip()
        for line in citation.evidence.splitlines()
        if line.partition(":")[0].strip() == field
    ]
    return any(
        exact_value_in_text_v3(
            fact.expected_value, value, numeric=fact.match_mode == "numeric_exact"
        )
        for value in values
    )
