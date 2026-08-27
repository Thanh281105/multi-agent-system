"""Public, provider-neutral orchestration result contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import AgentResult, DataProvenance, ExecutionPlan, TaskStatus
from app.shared.model_runtime import ModelCallMetadata


class RoutedIntent(BaseModel):
    """Deterministic intent and extracted entities used by the planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    confidence: float = Field(ge=0, le=1)
    entities: dict[str, object] = Field(default_factory=dict)
    routing_rule: str = Field(min_length=1, max_length=160)


class OrchestrationResult(BaseModel):
    """Complete turn result returned by the API gateway."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TaskStatus
    answer: str = Field(min_length=1)
    request_id: str
    trace_id: str
    session_id: str
    intent: str
    active_agent: str | None = None
    selected_product_id: int | None = Field(default=None, ge=1)
    plan: ExecutionPlan
    agent_results: tuple[AgentResult, ...] = ()
    provenance: tuple[DataProvenance, ...] = ()
    model_calls: tuple[ModelCallMetadata, ...] = ()
    warnings: tuple[str, ...] = ()
    duration_ms: float = Field(ge=0)
