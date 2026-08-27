"""Provider-neutral progress events for streaming orchestration status."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.contracts import TaskStatus


@dataclass(frozen=True, slots=True)
class OrchestrationProgress:
    """A safe status update; it never contains prompts or raw tool payloads."""

    phase: str
    message: str
    step_id: str | None = None
    agent_id: str | None = None
    status: TaskStatus | None = None


ProgressCallback = Callable[[OrchestrationProgress], Awaitable[None]]


async def emit_progress(
    callback: ProgressCallback | None,
    event: OrchestrationProgress,
) -> None:
    """Treat status delivery as best effort without hiding task cancellation."""

    if callback is None:
        return
    try:
        await callback(event)
    except Exception:
        return
