"""Per-session turn serialization for deterministic memory/state updates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Lock


@dataclass(slots=True)
class _TurnEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class SessionTurnCoordinator:
    """Serialize turns per session while allowing unrelated sessions in parallel."""

    def __init__(self) -> None:
        self._entries: dict[str, _TurnEntry] = {}
        self._guard = Lock()

    @asynccontextmanager
    async def turn(self, session_id: str) -> AsyncIterator[None]:
        with self._guard:
            entry = self._entries.setdefault(session_id, _TurnEntry())
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(session_id) is entry:
                    del self._entries[session_id]

    def active_session_count(self) -> int:
        with self._guard:
            return len(self._entries)
