"""Shared runtime services used by every agent and gateway layer."""

from app.shared.context import ExecutionContext, bind_execution_context
from app.shared.embedding_runtime import (
    EmbeddingRuntime,
    EmbeddingRuntimeError,
    OpenAIEmbeddingRuntime,
)
from app.shared.memory import InMemoryMemoryStore, MemoryEntry, MemoryRole, MemoryStore
from app.shared.model_runtime import (
    ModelCallMetadata,
    ModelRuntime,
    ModelRuntimeError,
    ModelRuntimeMode,
    OpenAIModelRuntime,
    ReasoningEffort,
    StructuredModelResult,
    collect_model_calls,
    mark_model_call_fallback,
)
from app.shared.redis_state import (
    RedisMemoryStore,
    RedisSessionStore,
    SharedStateUnavailableError,
)
from app.shared.session import (
    InMemorySessionStore,
    SessionExpiredError,
    SessionNotFoundError,
    SessionOwnershipError,
    SessionState,
    SessionStore,
)
from app.shared.telemetry import MetricRegistry, Telemetry, TraceEvent

__all__ = [
    "ExecutionContext",
    "EmbeddingRuntime",
    "EmbeddingRuntimeError",
    "InMemoryMemoryStore",
    "InMemorySessionStore",
    "MemoryEntry",
    "MemoryRole",
    "MemoryStore",
    "MetricRegistry",
    "ModelCallMetadata",
    "ModelRuntime",
    "ModelRuntimeError",
    "ModelRuntimeMode",
    "OpenAIEmbeddingRuntime",
    "OpenAIModelRuntime",
    "ReasoningEffort",
    "RedisMemoryStore",
    "RedisSessionStore",
    "SessionExpiredError",
    "SessionNotFoundError",
    "SessionOwnershipError",
    "SessionState",
    "SessionStore",
    "SharedStateUnavailableError",
    "Telemetry",
    "TraceEvent",
    "StructuredModelResult",
    "bind_execution_context",
    "collect_model_calls",
    "mark_model_call_fallback",
]
