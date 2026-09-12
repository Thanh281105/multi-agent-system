"""Safe live progress for v2 turns; durable state remains the replay source."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

from app.contracts import TaskStatus
from app.v2.contracts import TurnStatus


class TurnProgressPhase(StrEnum):
    """Real lifecycle boundaries that may be observed by JSON or SSE clients."""

    ADMITTED = "admitted"
    ATTACHED = "attached"
    CLAIMED = "claimed"
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class TurnProgress:
    """A safe status event without prompts, raw payloads, or answer text."""

    turn_id: str
    phase: TurnProgressPhase
    turn_status: TurnStatus
    step_id: str | None = None
    capability: str | None = None
    plan_revision: int | None = None
    step_status: TaskStatus | None = None
    reused: bool = False

    def __post_init__(self) -> None:
        step_phase = self.phase in {
            TurnProgressPhase.STEP_STARTED,
            TurnProgressPhase.STEP_FINISHED,
        }
        if step_phase != (self.step_id is not None):
            raise ValueError("step progress requires exactly one step identity")
        if step_phase != (self.capability is not None):
            raise ValueError("step progress requires exactly one capability")
        if step_phase != (self.plan_revision is not None):
            raise ValueError("step progress requires exactly one plan revision")
        if self.phase is TurnProgressPhase.STEP_FINISHED:
            if self.step_status is None:
                raise ValueError("finished step progress requires a terminal status")
        elif self.step_status is not None:
            raise ValueError("only finished step progress exposes a step status")
        if step_phase and self.turn_status is not TurnStatus.RUNNING:
            raise ValueError("step progress requires a running turn")
        terminal = self.turn_status in {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
        }
        if (self.phase is TurnProgressPhase.TERMINAL) != terminal:
            raise ValueError("terminal progress must match a terminal turn state")


ProgressCallback = Callable[[TurnProgress], Awaitable[None]]
CancellationProbe = Callable[[], bool]


async def emit_progress(
    callback: ProgressCallback | None,
    event: TurnProgress,
) -> None:
    """Deliver optional progress without making execution depend on a client."""

    if callback is None:
        return
    try:
        await callback(event)
    except Exception:
        return


@dataclass(frozen=True, slots=True)
class _ProgressContext:
    turn_id: str
    callback: ProgressCallback | None
    cancellation_requested: CancellationProbe | None


_current_progress: ContextVar[_ProgressContext | None] = ContextVar(
    "v2_turn_progress", default=None
)


@contextmanager
def turn_progress_scope(
    *,
    turn_id: str,
    callback: ProgressCallback | None,
    cancellation_requested: CancellationProbe | None = None,
) -> Iterator[None]:
    """Bind progress and cooperative cancellation to the claimed async task."""

    token = _current_progress.set(
        _ProgressContext(
            turn_id=turn_id,
            callback=callback,
            cancellation_requested=cancellation_requested,
        )
    )
    try:
        yield
    finally:
        _current_progress.reset(token)


def cancellation_requested() -> bool:
    context = _current_progress.get()
    return bool(
        context is not None
        and context.cancellation_requested is not None
        and context.cancellation_requested()
    )


async def emit_current_progress(
    *,
    phase: TurnProgressPhase,
    turn_status: TurnStatus,
    step_id: str | None = None,
    capability: str | None = None,
    plan_revision: int | None = None,
    step_status: TaskStatus | None = None,
    reused: bool = False,
) -> None:
    context = _current_progress.get()
    if context is None:
        return
    await emit_progress(
        context.callback,
        TurnProgress(
            turn_id=context.turn_id,
            phase=phase,
            turn_status=turn_status,
            step_id=step_id,
            capability=capability,
            plan_revision=plan_revision,
            step_status=step_status,
            reused=reused,
        ),
    )


class TurnProgressHub:
    """Fan out live events; reconnecting callers must query durable turn state."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[TurnProgress]]] = {}
        self._terminal_events: OrderedDict[str, None] = OrderedDict()

    async def publish(self, event: TurnProgress) -> None:
        if event.phase is TurnProgressPhase.TERMINAL:
            if event.turn_id in self._terminal_events:
                return
            self._terminal_events[event.turn_id] = None
            if len(self._terminal_events) > 4_096:
                self._terminal_events.popitem(last=False)
        for queue in tuple(self._subscribers.get(event.turn_id, ())):
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(
        self,
        turn_id: str,
    ) -> AsyncIterator[asyncio.Queue[TurnProgress]]:
        queue: asyncio.Queue[TurnProgress] = asyncio.Queue()
        subscribers = self._subscribers.setdefault(turn_id, set())
        subscribers.add(queue)
        try:
            yield queue
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(turn_id, None)


__all__ = [
    "CancellationProbe",
    "ProgressCallback",
    "TurnProgress",
    "TurnProgressHub",
    "TurnProgressPhase",
    "cancellation_requested",
    "emit_current_progress",
    "emit_progress",
    "turn_progress_scope",
]
