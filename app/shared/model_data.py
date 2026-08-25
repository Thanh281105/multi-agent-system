"""Bound and redact application facts before sending them to a model."""

from __future__ import annotations

from typing import Any

_REDACTED_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "private_key",
    "prompt",
    "secret",
    "token",
)


def bounded_model_data(value: object, *, depth: int = 0) -> Any:
    """Return a JSON-safe, size-bounded view with obvious secrets removed."""

    if depth >= 6:
        return "[depth-limited]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:600]
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for key, nested in list(value.items())[:40]:
            normalized = str(key).casefold()
            if any(part in normalized for part in _REDACTED_KEY_PARTS):
                continue
            if normalized == "reviews":
                reviews = nested if isinstance(nested, list) else []
                bounded["review_count"] = len(reviews)
                continue
            bounded[str(key)[:100]] = bounded_model_data(nested, depth=depth + 1)
        return bounded
    if isinstance(value, (list, tuple)):
        return [bounded_model_data(item, depth=depth + 1) for item in value[:10]]
    return str(value)[:200]
