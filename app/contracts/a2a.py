"""Typed in-process Agent-to-Agent contracts.

The modular monolith uses the same contracts that a future network transport
can serialize. Keeping transport details out of these models makes an A2A
deployment an adapter change instead of an agent rewrite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"
ACTION_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"


def utc_now() -> datetime:
    """Return an aware UTC timestamp for serializable contracts."""

    return datetime.now(UTC)


class TaskStatus(StrEnum):
    """Lifecycle states shared by plans, agent calls, and API responses."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class AgentError(BaseModel):
    """Stable, safe error exposed between platform components."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=ACTION_PATTERN)
    message: str = Field(min_length=1, max_length=500)
    source: str = Field(pattern=IDENTIFIER_PATTERN)
    retryable: bool = False


class DataProvenance(BaseModel):
    """Identifies where facts came from without exposing raw credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str = Field(pattern=ACTION_PATTERN)
    source_id: str = Field(min_length=1, max_length=200)
    fields: tuple[str, ...] = ()
    sample_data: bool = True
    observed_at: datetime = Field(default_factory=utc_now)


class AgentMessage(BaseModel):
    """One immutable task sent through the in-process A2A dispatcher."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    task_id: str = Field(pattern=IDENTIFIER_PATTERN)
    session_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    trace_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source: str = Field(pattern=IDENTIFIER_PATTERN)
    target: str = Field(pattern=IDENTIFIER_PATTERN)
    action: str = Field(pattern=ACTION_PATTERN)
    payload: dict[str, Any] = Field(
        default_factory=dict,
        alias="input",
        serialization_alias="input",
    )
    created_at: datetime = Field(default_factory=utc_now)


class AgentResult(BaseModel):
    """Grounded result returned by a domain agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=IDENTIFIER_PATTERN)
    agent_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: TaskStatus
    data: dict[str, Any] = Field(default_factory=dict)
    errors: tuple[AgentError, ...] = ()
    provenance: tuple[DataProvenance, ...] = ()
    duration_ms: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_status_payload(self) -> AgentResult:
        if self.status == TaskStatus.FAILED and not self.errors:
            raise ValueError("failed results must include at least one error")
        if self.status == TaskStatus.SUCCESS and self.errors:
            raise ValueError("successful results cannot include errors")
        return self


class ExecutionStep(BaseModel):
    """A validated unit of work in an orchestrator plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(pattern=IDENTIFIER_PATTERN)
    agent_id: str = Field(pattern=IDENTIFIER_PATTERN)
    action: str = Field(pattern=ACTION_PATTERN)
    depends_on: tuple[str, ...] = ()
    input: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    """Serializable DAG produced by the orchestrator planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    intent: str = Field(pattern=ACTION_PATTERN)
    steps: tuple[ExecutionStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dependencies(self) -> ExecutionPlan:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("execution step IDs must be unique")

        seen: set[str] = set()
        for step in self.steps:
            missing = set(step.depends_on) - seen
            if missing:
                raise ValueError(
                    f"step {step.step_id} has unresolved dependencies: "
                    f"{sorted(missing)}"
                )
            seen.add(step.step_id)
        return self
