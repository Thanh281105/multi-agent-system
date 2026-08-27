"""Bounded short-term conversation memory with an adapter-friendly port."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class MemoryRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MemoryEntry(BaseModel):
    """A compact turn summary, not hidden model reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: MemoryRole
    summary: str = Field(min_length=1, max_length=2_000)
    agent_id: str | None = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryStore(Protocol):
    def append(self, *, session_id: str, entry: MemoryEntry) -> None: ...

    def recent(
        self, *, session_id: str, limit: int = 10
    ) -> tuple[MemoryEntry, ...]: ...


class InMemoryMemoryStore:
    """Thread-safe bounded adapter used until Redis/PostgreSQL is configured."""

    def __init__(self, *, max_entries_per_session: int = 40) -> None:
        if max_entries_per_session < 1:
            raise ValueError("max_entries_per_session must be positive")
        self._capacity = max_entries_per_session
        self._entries: dict[str, deque[MemoryEntry]] = defaultdict(
            lambda: deque(maxlen=self._capacity)
        )
        self._lock = RLock()

    def append(self, *, session_id: str, entry: MemoryEntry) -> None:
        with self._lock:
            self._entries[session_id].append(entry)

    def recent(self, *, session_id: str, limit: int = 10) -> tuple[MemoryEntry, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            entries = self._entries.get(session_id)
            if entries is None:
                return ()
            return tuple(list(entries)[-limit:])

    def clear(self, *, session_id: str) -> None:
        with self._lock:
            self._entries.pop(session_id, None)
