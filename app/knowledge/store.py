"""Small runtime contract for optional knowledge backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KnowledgeStore(Protocol):
    """Readiness boundary implemented by disabled and future RAG adapters."""

    backend: str

    def ready(self) -> bool: ...


class DisabledKnowledgeStore:
    """No-op adapter used until a real, explicitly injected RAG backend exists."""

    backend = "disabled"

    def ready(self) -> bool:
        return True
