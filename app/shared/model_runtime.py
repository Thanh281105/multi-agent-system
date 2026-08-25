"""Provider-neutral structured model runtime with bounded OpenAI reliability.

The runtime deliberately exposes parsed application data and safe operational
metadata only. Prompts, model prose, provider response objects, credentials,
and hidden reasoning never cross this boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Any, Generic, Literal, Protocol, TypeVar
from uuid import uuid4

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field

from app.shared.context import current_execution_context
from app.shared.telemetry import Telemetry

StructuredT = TypeVar("StructuredT", bound=BaseModel)
ModelRuntimeMode = Literal["off", "shadow", "hybrid", "required"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]


class ModelCallMetadata(BaseModel):
    """Sanitized metadata suitable for traces and the public debug surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(pattern=r"^mcall_[a-f0-9]{32}$")
    stage: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    provider: Literal["openai"] = "openai"
    model: str = Field(min_length=1, max_length=120)
    status: Literal["success", "failed"]
    duration_ms: float = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    attempts: int = Field(ge=0, le=10)
    fallback_used: bool = False
    fallback_reason: str | None = Field(default=None, max_length=80)
    error_code: str | None = Field(default=None, max_length=80)


@dataclass(frozen=True, slots=True)
class StructuredModelResult(Generic[StructuredT]):
    """One schema-validated model result and its redacted call metadata."""

    value: StructuredT
    metadata: ModelCallMetadata


class ModelRuntimeError(RuntimeError):
    """Stable model failure that carries no provider response text."""

    def __init__(self, code: str, metadata: ModelCallMetadata) -> None:
        super().__init__(code)
        self.code = code
        self.metadata = metadata


class ModelRuntime(Protocol):
    """Application boundary for schema-constrained language-model calls."""

    async def generate_structured(
        self,
        *,
        stage: str,
        agent_id: str,
        model: str,
        instructions: str,
        input_text: str,
        schema: type[StructuredT],
        max_output_tokens: int | None = None,
        reasoning_effort: ReasoningEffort = "low",
    ) -> StructuredModelResult[StructuredT]: ...


_model_call_collector: ContextVar[list[ModelCallMetadata] | None] = ContextVar(
    "model_call_collector",
    default=None,
)


@contextmanager
def collect_model_calls() -> Iterator[list[ModelCallMetadata]]:
    """Collect model calls for one orchestration turn without global state."""

    calls: list[ModelCallMetadata] = []
    token = _model_call_collector.set(calls)
    try:
        yield calls
    finally:
        _model_call_collector.reset(token)


def mark_model_call_fallback(
    metadata: ModelCallMetadata,
    reason: str,
) -> ModelCallMetadata:
    """Mark an already-recorded call as ignored/fallen back in the collector."""

    updated = metadata.model_copy(
        update={"fallback_used": True, "fallback_reason": reason[:80]}
    )
    collector = _model_call_collector.get()
    if collector is not None:
        for index, item in enumerate(collector):
            if item.call_id == metadata.call_id:
                collector[index] = updated
                break
    return updated


class OpenAIModelRuntime:
    """Responses API adapter with local retry, timeout, and circuit breaking."""

    def __init__(
        self,
        api_key: str,
        *,
        client: Any | None = None,
        telemetry: Telemetry | None = None,
        timeout_seconds: float = 18.0,
        max_retries: int = 2,
        max_output_tokens: int = 1_200,
        max_concurrency: int = 8,
        circuit_failure_threshold: int = 4,
        circuit_recovery_seconds: float = 30.0,
    ) -> None:
        if not api_key.strip() and client is None:
            raise ValueError("OpenAIModelRuntime requires an API key")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if not 64 <= max_output_tokens <= 16_384:
            raise ValueError("max_output_tokens must be between 64 and 16384")
        if not 1 <= max_concurrency <= 64:
            raise ValueError("max_concurrency must be between 1 and 64")
        if not 1 <= circuit_failure_threshold <= 20:
            raise ValueError("circuit_failure_threshold must be between 1 and 20")
        if not 1 <= circuit_recovery_seconds <= 600:
            raise ValueError("circuit_recovery_seconds must be between 1 and 600")

        self._client = client or AsyncOpenAI(api_key=api_key, max_retries=0)
        self._telemetry = telemetry
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._max_output_tokens = max_output_tokens
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_recovery_seconds = circuit_recovery_seconds
        self._circuit_lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None

    async def generate_structured(
        self,
        *,
        stage: str,
        agent_id: str,
        model: str,
        instructions: str,
        input_text: str,
        schema: type[StructuredT],
        max_output_tokens: int | None = None,
        reasoning_effort: ReasoningEffort = "low",
    ) -> StructuredModelResult[StructuredT]:
        call_id = f"mcall_{uuid4().hex}"
        started_at = perf_counter()
        attempts = 0
        await self._assert_circuit_available(
            call_id=call_id,
            stage=stage,
            agent_id=agent_id,
            model=model,
            started_at=started_at,
        )
        last_error: BaseException | None = None

        async with self._semaphore:
            for attempt in range(self._max_retries + 1):
                attempts = attempt + 1
                try:
                    async with asyncio.timeout(self._timeout_seconds):
                        response = await self._client.responses.parse(
                            model=model,
                            instructions=instructions,
                            input=input_text,
                            text_format=schema,
                            max_output_tokens=(
                                max_output_tokens or self._max_output_tokens
                            ),
                            reasoning={"effort": reasoning_effort},
                            metadata=self._request_metadata(stage, agent_id),
                            safety_identifier=self._safety_identifier(),
                            store=False,
                            timeout=self._timeout_seconds,
                        )
                    if getattr(response, "status", None) != "completed":
                        raise ValueError("model_response_incomplete")
                    value = getattr(response, "output_parsed", None)
                    if not isinstance(value, schema):
                        raise ValueError("model_response_contract_violation")
                    metadata = self._metadata(
                        call_id=call_id,
                        stage=stage,
                        agent_id=agent_id,
                        model=model,
                        status="success",
                        duration_ms=(perf_counter() - started_at) * 1_000,
                        attempts=attempts,
                        response=response,
                    )
                    await self._record_success(metadata)
                    return StructuredModelResult(value=value, metadata=metadata)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    last_error = exc
                    if attempt >= self._max_retries or not self._is_retryable(exc):
                        break
                    await asyncio.sleep(min(0.15 * (2**attempt), 1.2))

        code = self._error_code(last_error)
        metadata = self._metadata(
            call_id=call_id,
            stage=stage,
            agent_id=agent_id,
            model=model,
            status="failed",
            duration_ms=(perf_counter() - started_at) * 1_000,
            attempts=attempts,
            error_code=code,
        )
        await self._record_failure(metadata)
        raise ModelRuntimeError(code, metadata) from last_error

    async def _assert_circuit_available(
        self,
        *,
        call_id: str,
        stage: str,
        agent_id: str,
        model: str,
        started_at: float,
    ) -> None:
        async with self._circuit_lock:
            if self._circuit_opened_at is None:
                return
            elapsed = monotonic() - self._circuit_opened_at
            if elapsed >= self._circuit_recovery_seconds:
                self._circuit_opened_at = None
                self._consecutive_failures = 0
                return
        metadata = self._metadata(
            call_id=call_id,
            stage=stage,
            agent_id=agent_id,
            model=model,
            status="failed",
            duration_ms=(perf_counter() - started_at) * 1_000,
            attempts=0,
            error_code="model_circuit_open",
        )
        self._record(metadata)
        raise ModelRuntimeError("model_circuit_open", metadata)

    async def _record_success(self, metadata: ModelCallMetadata) -> None:
        async with self._circuit_lock:
            self._consecutive_failures = 0
            self._circuit_opened_at = None
        self._record(metadata)

    async def _record_failure(self, metadata: ModelCallMetadata) -> None:
        async with self._circuit_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._circuit_failure_threshold:
                self._circuit_opened_at = monotonic()
        self._record(metadata)

    def _record(self, metadata: ModelCallMetadata) -> None:
        collector = _model_call_collector.get()
        if collector is not None:
            collector.append(metadata)
        context = current_execution_context()
        if self._telemetry is None or context is None:
            return
        attributes: dict[str, str | int | float | bool] = {
            "call_id": metadata.call_id,
            "stage": metadata.stage,
            "provider": metadata.provider,
            "model": metadata.model,
            "input_tokens": metadata.input_tokens,
            "output_tokens": metadata.output_tokens,
            "total_tokens": metadata.total_tokens,
            "attempts": metadata.attempts,
        }
        if metadata.error_code:
            attributes["error_code"] = metadata.error_code
        self._telemetry.record(
            context.child(agent_id=metadata.agent_id),
            component="model_runtime",
            operation=metadata.stage,
            outcome=metadata.status,
            duration_ms=metadata.duration_ms,
            attributes=attributes,
        )

    @staticmethod
    def _request_metadata(stage: str, agent_id: str) -> dict[str, str]:
        context = current_execution_context()
        metadata = {"stage": stage, "agent_id": agent_id}
        if context is not None:
            metadata.update(
                {
                    "request_id": context.request_id,
                    "trace_id": context.trace_id,
                }
            )
        return metadata

    @staticmethod
    def _safety_identifier() -> str | None:
        context = current_execution_context()
        if context is None:
            return None
        digest = hashlib.sha256(context.principal_id.encode("utf-8")).hexdigest()
        return f"usr_{digest[:32]}"

    @staticmethod
    def _metadata(
        *,
        call_id: str,
        stage: str,
        agent_id: str,
        model: str,
        status: Literal["success", "failed"],
        duration_ms: float,
        attempts: int,
        response: Any | None = None,
        error_code: str | None = None,
    ) -> ModelCallMetadata:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", input_tokens + output_tokens) or 0
        )
        return ModelCallMetadata(
            call_id=call_id,
            stage=stage,
            agent_id=agent_id,
            model=model,
            status=status,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            attempts=attempts,
            error_code=error_code,
        )

    @staticmethod
    def _is_retryable(error: BaseException) -> bool:
        if isinstance(
            error,
            (
                TimeoutError,
                APITimeoutError,
                APIConnectionError,
                RateLimitError,
                InternalServerError,
            ),
        ):
            return True
        return isinstance(error, APIStatusError) and error.status_code in {
            408,
            409,
            429,
            500,
            502,
            503,
            504,
        }

    @staticmethod
    def _error_code(error: BaseException | None) -> str:
        if isinstance(error, (TimeoutError, APITimeoutError)):
            return "model_timeout"
        if isinstance(error, RateLimitError):
            return "model_rate_limited"
        if isinstance(error, APIConnectionError):
            return "model_connection_failed"
        if isinstance(error, APIStatusError):
            return "model_provider_error"
        if isinstance(error, ValueError):
            return str(error)[:80]
        return "model_runtime_failed"
