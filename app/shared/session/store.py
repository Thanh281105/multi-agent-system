"""User-owned session lifecycle with TTL and optimistic revision metadata."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class SessionNotFoundError(LookupError):
    """Raised when a requested session does not exist."""


class SessionExpiredError(LookupError):
    """Raised when a requested session has passed its TTL."""


class SessionOwnershipError(PermissionError):
    """Raised when a principal tries to reuse another principal's session."""


class SessionState(BaseModel):
    """Immutable state stored behind a SessionStore adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(pattern=r"^sess_[a-zA-Z0-9_-]{3,120}$")
    owner_id: str = Field(min_length=1, max_length=160)
    active_agent: str | None = Field(default=None, max_length=128)
    last_intent: str | None = Field(default=None, max_length=128)
    state: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class SessionStore(Protocol):
    """Port implemented by local and future Redis session adapters."""

    def create(
        self, *, owner_id: str, session_id: str | None = None
    ) -> SessionState: ...

    def get(self, *, owner_id: str, session_id: str) -> SessionState: ...

    def update(
        self,
        *,
        owner_id: str,
        session_id: str,
        active_agent: str | None = None,
        last_intent: str | None = None,
        state_patch: dict[str, Any] | None = None,
    ) -> SessionState: ...


class InMemorySessionStore:
    """Thread-safe local adapter suitable for tests and one-process demos."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 3_600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sessions: dict[str, SessionState] = {}
        self._lock = RLock()

    def create(self, *, owner_id: str, session_id: str | None = None) -> SessionState:
        now = self._aware_now()
        target_id = session_id or f"sess_{uuid4().hex}"
        with self._lock:
            existing = self._sessions.get(target_id)
            if existing is not None:
                self._assert_owner(existing, owner_id)
                if existing.expires_at <= now:
                    del self._sessions[target_id]
                else:
                    return existing
            session = SessionState(
                session_id=target_id,
                owner_id=owner_id,
                created_at=now,
                updated_at=now,
                expires_at=now + self._ttl,
            )
            self._sessions[target_id] = session
            return session

    def get(self, *, owner_id: str, session_id: str) -> SessionState:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            self._assert_owner(session, owner_id)
            if session.expires_at <= self._aware_now():
                del self._sessions[session_id]
                raise SessionExpiredError(session_id)
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
        with self._lock:
            current = self.get(owner_id=owner_id, session_id=session_id)
            now = self._aware_now()
            updated_state = dict(current.state)
            if state_patch:
                updated_state.update(state_patch)
            updated = current.model_copy(
                update={
                    "active_agent": (
                        active_agent
                        if active_agent is not None
                        else current.active_agent
                    ),
                    "last_intent": (
                        last_intent if last_intent is not None else current.last_intent
                    ),
                    "state": updated_state,
                    "revision": current.revision + 1,
                    "updated_at": now,
                    "expires_at": now + self._ttl,
                }
            )
            self._sessions[session_id] = updated
            return updated

    def delete(self, *, owner_id: str, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            self._assert_owner(session, owner_id)
            del self._sessions[session_id]

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    @staticmethod
    def _assert_owner(session: SessionState, owner_id: str) -> None:
        if session.owner_id != owner_id:
            raise SessionOwnershipError(session.session_id)

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("session clock must return an aware datetime")
        return now
