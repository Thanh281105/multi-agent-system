"""Per-session turn serialization for deterministic memory/state updates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from redis.exceptions import RedisError, WatchError

from app.shared import SharedStateUnavailableError


class SessionTurnBusyError(RuntimeError):
    """Raised when another worker holds the same session turn lock."""


class TurnCoordinator(Protocol):
    """Port implemented by local and distributed turn serializers."""

    def turn(self, session_id: str) -> Any: ...


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


class RedisSessionTurnCoordinator:
    """Serialize a session across workers with a leased Redis lock."""

    def __init__(
        self,
        client: Any,
        *,
        key_prefix: str = "ecommerce_agents",
        lease_seconds: float = 40,
        wait_seconds: float = 5,
        poll_seconds: float = 0.05,
    ) -> None:
        if lease_seconds <= 0 or wait_seconds < 0 or poll_seconds <= 0:
            raise ValueError("Redis turn lock timings are invalid")
        self._client = client
        self._prefix = key_prefix
        self._lease_ms = max(1, int(lease_seconds * 1_000))
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds

    @asynccontextmanager
    async def turn(self, session_id: str) -> AsyncIterator[None]:
        key = f"{self._prefix}:turn:{session_id}"
        token = uuid4().hex
        deadline = monotonic() + self._wait_seconds
        while True:
            try:
                acquired = await asyncio.to_thread(
                    self._client.set,
                    key,
                    token,
                    nx=True,
                    px=self._lease_ms,
                )
            except RedisError as exc:
                raise SharedStateUnavailableError(
                    "redis turn lock acquisition failed"
                ) from exc
            if acquired:
                break
            if monotonic() >= deadline:
                raise SessionTurnBusyError(session_id)
            await asyncio.sleep(self._poll_seconds)

        try:
            yield
        finally:
            await asyncio.to_thread(self._release, key, token)

    def _release(self, key: str, token: str) -> None:
        try:
            for _ in range(3):
                pipeline = self._client.pipeline()
                try:
                    pipeline.watch(key)
                    if pipeline.get(key) != token:
                        return
                    pipeline.multi()
                    pipeline.delete(key)
                    pipeline.execute()
                    return
                except WatchError:
                    continue
                finally:
                    pipeline.reset()
        except RedisError as exc:
            raise SharedStateUnavailableError("redis turn lock release failed") from exc
