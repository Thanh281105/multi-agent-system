"""Standards-compliant, injection-safe Server-Sent Event encoding."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def encode_sse(
    event: str,
    data: BaseModel | dict[str, Any],
    *,
    event_id: str | None = None,
    retry_ms: int | None = None,
) -> str:
    """Serialize data as one JSON line and delimit exactly one SSE event."""

    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines.append(f"event: {event}")
    lines.append(
        "data: "
        + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return "\n".join(lines) + "\n\n"


def heartbeat() -> str:
    """SSE comment used to keep proxies from closing an idle stream."""

    return ": heartbeat\n\n"
