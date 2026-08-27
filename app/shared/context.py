"""Execution correlation context propagated across platform boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator
from uuid import uuid4

from app.contracts import AuthorizationContext


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Immutable request identity shared by gateway, orchestrator, and agents."""

    principal_id: str
    request_id: str
    trace_id: str
    session_id: str
    task_id: str
    authorization: AuthorizationContext
    agent_id: str = "orchestrator"

    @classmethod
    def create(
        cls,
        *,
        principal_id: str,
        session_id: str,
        authorization: AuthorizationContext | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> ExecutionContext:
        effective_authorization = authorization or AuthorizationContext(
            principal_id=principal_id,
            scopes=frozenset(),
        )
        if effective_authorization.principal_id != principal_id:
            raise ValueError(
                "authorization principal does not match execution principal"
            )
        return cls(
            principal_id=principal_id,
            request_id=request_id or _new_id("req"),
            trace_id=trace_id or _new_id("trace"),
            session_id=session_id,
            task_id=_new_id("task"),
            authorization=effective_authorization,
        )

    def child(self, *, agent_id: str, task_id: str | None = None) -> ExecutionContext:
        return replace(
            self,
            agent_id=agent_id,
            task_id=task_id or _new_id("task"),
        )

    def correlation_fields(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
        }


_current_context: ContextVar[ExecutionContext | None] = ContextVar(
    "execution_context",
    default=None,
)


@contextmanager
def bind_execution_context(context: ExecutionContext) -> Iterator[None]:
    """Bind a context for structured logging and reset it reliably afterward."""

    token = _current_context.set(context)
    try:
        yield
    finally:
        _current_context.reset(token)


def current_execution_context() -> ExecutionContext | None:
    return _current_context.get()
