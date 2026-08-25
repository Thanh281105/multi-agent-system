"""Public contracts for versioned chat, SSE, and stable errors."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.contracts import TaskStatus
from app.orchestrator import OrchestrationResult


class GatewayChatRequest(BaseModel):
    """Validated, bounded user input accepted by the versioned gateway."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2_000)
    session_id: str | None = Field(
        default=None,
        pattern=r"^sess_[a-zA-Z0-9_-]{3,120}$",
    )

    @field_validator("message")
    @classmethod
    def strip_nonempty_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message không được để trống")
        return cleaned


class AgentExecutionInfo(BaseModel):
    """Safe execution metadata without prompts or raw tool payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    agent_id: str
    action: str
    depends_on: tuple[str, ...] = ()
    status: TaskStatus
    duration_ms: float = Field(ge=0)
    error_codes: tuple[str, ...] = ()


class ProvenanceInfo(BaseModel):
    """Source metadata exposed so sample evidence is never mistaken for real data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str
    source_id: str
    fields: tuple[str, ...] = ()
    sample_data: bool
    observed_at: datetime


class ModelCallInfo(BaseModel):
    """Sanitized model telemetry; prompts and provider payloads stay private."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    stage: str
    agent_id: str
    provider: str
    model: str
    response_id: str | None = None
    status: str
    duration_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    attempts: int = Field(ge=0)
    fallback_used: bool
    fallback_reason: str | None = None
    error_code: str | None = None


class GatewayChatResponse(BaseModel):
    """Grounded multi-agent result returned from both JSON and SSE endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: str = "v1"
    status: TaskStatus
    answer: str
    session_id: str
    request_id: str
    trace_id: str
    intent: str
    active_agent: str | None = None
    selected_product_id: int | None = None
    executions: tuple[AgentExecutionInfo, ...] = ()
    provenance: tuple[ProvenanceInfo, ...] = ()
    model_calls: tuple[ModelCallInfo, ...] = ()
    warnings: tuple[str, ...] = ()
    sample_data: bool = True
    duration_ms: float = Field(ge=0)


class GatewayErrorDetail(BaseModel):
    """Stable safe error independent of internal exception classes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    request_id: str
    trace_id: str
    retryable: bool = False
    validation_errors: tuple[dict[str, object], ...] = ()


class GatewayErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: GatewayErrorDetail


class GatewayStatusEvent(BaseModel):
    """One ordered SSE progress event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    phase: str
    message: str
    request_id: str
    trace_id: str
    step_id: str | None = None
    agent_id: str | None = None
    status: TaskStatus | None = None


def build_chat_response(result: OrchestrationResult) -> GatewayChatResponse:
    """Project internal orchestration details onto a stable public contract."""

    executions = tuple(
        AgentExecutionInfo(
            step_id=step.step_id,
            agent_id=step.agent_id,
            action=step.action,
            depends_on=step.depends_on,
            status=agent_result.status,
            duration_ms=agent_result.duration_ms,
            error_codes=tuple(error.code for error in agent_result.errors),
        )
        for step, agent_result in zip(
            result.plan.steps,
            result.agent_results,
            strict=True,
        )
    )
    provenance = tuple(
        ProvenanceInfo(
            source_type=item.source_type,
            source_id=item.source_id,
            fields=item.fields,
            sample_data=item.sample_data,
            observed_at=item.observed_at,
        )
        for item in result.provenance
    )
    model_calls = tuple(
        ModelCallInfo.model_validate(item.model_dump()) for item in result.model_calls
    )
    return GatewayChatResponse(
        status=result.status,
        answer=result.answer,
        session_id=result.session_id,
        request_id=result.request_id,
        trace_id=result.trace_id,
        intent=result.intent,
        active_agent=result.active_agent,
        selected_product_id=result.selected_product_id,
        executions=executions,
        provenance=provenance,
        model_calls=model_calls,
        warnings=result.warnings,
        sample_data=all(item.sample_data for item in result.provenance),
        duration_ms=result.duration_ms,
    )
