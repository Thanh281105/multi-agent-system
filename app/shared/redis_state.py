"""Redis-backed session and bounded short-term memory adapters."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from redis.exceptions import RedisError, WatchError

from app.shared.memory.store import MemoryEntry
from app.shared.session.store import (
    SessionNotFoundError,
    SessionOwnershipError,
    SessionState,
)


class SharedStateUnavailableError(RuntimeError):
    """Raised when Redis state cannot be read safely or atomically."""


class RedisSessionStore:
    """Persistent owner-bound session state with optimistic Redis updates."""

    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: int = 3_600,
        key_prefix: str = "ecommerce_agents",
        clock: Callable[[], datetime] | None = None,
        update_retries: int = 5,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if update_retries < 1:
            raise ValueError("update_retries must be positive")
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._ttl = timedelta(seconds=ttl_seconds)
        self._prefix = key_prefix
        self._clock = clock or (lambda: datetime.now(UTC))
        self._update_retries = update_retries

    def create(self, *, owner_id: str, session_id: str | None = None) -> SessionState:
        now = self._aware_now()
        target_id = session_id or f"sess_{uuid4().hex}"
        key = self._key(target_id)
        try:
            existing_raw = self._client.get(key)
            if existing_raw is not None:
                existing = self._decode_session(existing_raw)
                self._assert_owner(existing, owner_id)
                return existing

            session = SessionState(
                session_id=target_id,
                owner_id=owner_id,
                created_at=now,
                updated_at=now,
                expires_at=now + self._ttl,
            )
            created = self._client.set(
                key,
                session.model_dump_json(),
                ex=self._ttl_seconds,
                nx=True,
            )
            if created:
                return session
            return self.get(owner_id=owner_id, session_id=target_id)
        except (SessionOwnershipError, SharedStateUnavailableError):
            raise
        except RedisError as exc:
            raise SharedStateUnavailableError("redis session create failed") from exc

    def get(self, *, owner_id: str, session_id: str) -> SessionState:
        try:
            raw = self._client.get(self._key(session_id))
        except RedisError as exc:
            raise SharedStateUnavailableError("redis session read failed") from exc
        if raw is None:
            raise SessionNotFoundError(session_id)
        session = self._decode_session(raw)
        self._assert_owner(session, owner_id)
        return session

    def update(
        self,
        *,
        owner_id: str,
        session_id: str,
        active_agent: str | None = None,
        last_intent: str | None = None,
        state_patch: dict[str, Any] | None = None,
    ) -> SessionState:
        key = self._key(session_id)
        try:
            for _ in range(self._update_retries):
                pipeline = self._client.pipeline()
                try:
                    pipeline.watch(key)
                    raw = pipeline.get(key)
                    if raw is None:
                        raise SessionNotFoundError(session_id)
                    current = self._decode_session(raw)
                    self._assert_owner(current, owner_id)
                    updated_state = dict(current.state)
                    if state_patch:
                        updated_state.update(state_patch)
                    now = self._aware_now()
                    updated = current.model_copy(
                        update={
                            "active_agent": (
                                active_agent
                                if active_agent is not None
                                else current.active_agent
                            ),
                            "last_intent": (
                                last_intent
                                if last_intent is not None
                                else current.last_intent
                            ),
                            "state": updated_state,
                            "revision": current.revision + 1,
                            "updated_at": now,
                            "expires_at": now + self._ttl,
                        }
                    )
                    pipeline.multi()
                    pipeline.set(
                        key,
                        updated.model_dump_json(),
                        ex=self._ttl_seconds,
                    )
                    pipeline.execute()
                    return updated
                except WatchError:
                    continue
                finally:
                    pipeline.reset()
        except (
            SessionNotFoundError,
            SessionOwnershipError,
            SharedStateUnavailableError,
        ):
            raise
        except RedisError as exc:
            raise SharedStateUnavailableError("redis session update failed") from exc
        raise SharedStateUnavailableError("redis session update contention exceeded")

    def delete(self, *, owner_id: str, session_id: str) -> None:
        session = self.get(owner_id=owner_id, session_id=session_id)
        try:
            self._client.delete(self._key(session.session_id))
        except RedisError as exc:
            raise SharedStateUnavailableError("redis session delete failed") from exc

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}:session:{session_id}"

    @staticmethod
    def _decode_session(raw: Any) -> SessionState:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                raise TypeError("redis session payload must be text")
            return SessionState.model_validate_json(raw)
        except (UnicodeDecodeError, TypeError, ValidationError) as exc:
            raise SharedStateUnavailableError(
                "redis session payload is invalid"
            ) from exc

    @staticmethod
    def _assert_owner(session: SessionState, owner_id: str) -> None:
        if session.owner_id != owner_id:
            raise SessionOwnershipError(session.session_id)

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("session clock must return an aware datetime")
        return now


class RedisMemoryStore:
    """Bounded conversation memory stored as an expiring Redis list."""

    def __init__(
        self,
        client: Any,
        *,
        max_entries_per_session: int = 40,
        ttl_seconds: int = 3_600,
        key_prefix: str = "ecommerce_agents",
    ) -> None:
        if max_entries_per_session < 1:
            raise ValueError("max_entries_per_session must be positive")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._client = client
        self._capacity = max_entries_per_session
        self._ttl_seconds = ttl_seconds
        self._prefix = key_prefix

    def append(self, *, session_id: str, entry: MemoryEntry) -> None:
        try:
            pipeline = self._client.pipeline(transaction=True)
            pipeline.rpush(self._key(session_id), entry.model_dump_json())
            pipeline.ltrim(self._key(session_id), -self._capacity, -1)
            pipeline.expire(self._key(session_id), self._ttl_seconds)
            pipeline.execute()
        except RedisError as exc:
            raise SharedStateUnavailableError("redis memory append failed") from exc

    def recent(self, *, session_id: str, limit: int = 10) -> tuple[MemoryEntry, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        try:
            raw_entries = self._client.lrange(self._key(session_id), -limit, -1)
            return tuple(self._decode_entry(raw) for raw in raw_entries)
        except SharedStateUnavailableError:
            raise
        except RedisError as exc:
            raise SharedStateUnavailableError("redis memory read failed") from exc

    def clear(self, *, session_id: str) -> None:
        try:
            self._client.delete(self._key(session_id))
        except RedisError as exc:
            raise SharedStateUnavailableError("redis memory clear failed") from exc

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}:memory:{session_id}"

    @staticmethod
    def _decode_entry(raw: Any) -> MemoryEntry:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                raise TypeError("redis memory payload must be text")
            return MemoryEntry.model_validate_json(raw)
        except (UnicodeDecodeError, TypeError, ValidationError) as exc:
            raise SharedStateUnavailableError(
                "redis memory payload is invalid"
            ) from exc
