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
from urllib.parse import urlparse
from uuid import uuid4

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.shared.budget import (
    BudgetCancelledError,
    BudgetDuplicateAttemptError,
    BudgetError,
    BudgetLimitExceededError,
    PricingManifestError,
    ProviderBudgetContext,
    current_provider_budget,
    generation_payload_token_bound,
    generation_usage_from_response,
    request_fingerprint,
    seconds_until,
)
from app.shared.context import current_execution_context
from app.shared.telemetry import Telemetry

StructuredT = TypeVar("StructuredT", bound=BaseModel)
ModelRuntimeMode = Literal["off", "shadow", "hybrid", "required"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]
ModelErrorCode = Literal[
    "model_timeout",
    "model_rate_limited",
    "model_connection_failed",
    "model_provider_error",
    "model_response_incomplete",
    "model_response_contract_violation",
    "model_response_invalid",
    "model_runtime_failed",
    "model_circuit_open",
]


class ModelCallMetadata(BaseModel):
    """Sanitized metadata suitable for traces and the public debug surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(pattern=r"^mcall_[a-f0-9]{32}$")
    stage: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    provider: Literal["openai"] = "openai"
    model: str = Field(min_length=1, max_length=120)
    response_id: str | None = Field(default=None, min_length=1, max_length=256)
    status: Literal["success", "failed"]
    duration_ms: float = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    attempts: int = Field(ge=0, le=10)
    fallback_used: bool = False
    fallback_reason: str | None = Field(default=None, max_length=80)
    error_code: ModelErrorCode | None = None

    @model_validator(mode="after")
    def validate_usage(self) -> ModelCallMetadata:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens must be included in output tokens")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total tokens must equal input plus output tokens")
        return self


@dataclass(frozen=True, slots=True)
class StructuredModelResult(Generic[StructuredT]):
    """One schema-validated model result and its redacted call metadata."""

    value: StructuredT
    metadata: ModelCallMetadata


class ModelRuntimeError(RuntimeError):
    """Stable model failure that carries no provider response text."""

    def __init__(self, code: ModelErrorCode, metadata: ModelCallMetadata) -> None:
        super().__init__(code)
        self.code = code
        self.metadata = metadata


class _ModelResponseError(ValueError):
    """Internal response-state failure carrying only an allowlisted code."""

    def __init__(
        self,
        code: Literal[
            "model_response_incomplete",
            "model_response_contract_violation",
        ],
    ) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _BudgetedGenerationRequest:
    provider_kwargs: dict[str, Any]
    input_token_bound: int
    output_token_bound: int
    fingerprint_sha256: str


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

    parent = _model_call_collector.get()
    calls: list[ModelCallMetadata] = []
    token = _model_call_collector.set(calls)
    try:
        yield calls
    finally:
        _model_call_collector.reset(token)
        if parent is not None:
            positions = {item.call_id: index for index, item in enumerate(parent)}
            for item in calls:
                index = positions.get(item.call_id)
                if index is None:
                    positions[item.call_id] = len(parent)
                    parent.append(item)
                else:
                    parent[index] = item


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
        self._budget_client: Any | None = None
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
        budget = current_provider_budget()
        call_id = (
            budget.new_call_id("generation")
            if budget is not None
            else f"mcall_{uuid4().hex}"
        )
        started_at = perf_counter()
        attempts = 0
        budget_request = (
            self._prepare_budgeted_request(
                budget=budget,
                model=model,
                stage=stage,
                agent_id=agent_id,
                instructions=instructions,
                input_text=input_text,
                schema=schema,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
            )
            if budget is not None
            else None
        )
        await self._assert_circuit_available(
            call_id=call_id,
            stage=stage,
            agent_id=agent_id,
            model=model,
            started_at=started_at,
        )
        last_error: BaseException | None = None
        retry_count = (
            self._max_retries
            if budget is None
            else min(self._max_retries, budget.max_retries)
        )

        async with self._semaphore:
            for attempt in range(retry_count + 1):
                attempts = attempt + 1
                try:
                    if budget is None:
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
                            raise _ModelResponseError("model_response_incomplete")
                        value = getattr(response, "output_parsed", None)
                        if not isinstance(value, schema):
                            raise _ModelResponseError(
                                "model_response_contract_violation"
                            )
                    else:
                        assert budget_request is not None
                        response, value = await self._budgeted_generation_attempt(
                            budget=budget,
                            call_id=call_id,
                            attempt_number=attempts,
                            request=budget_request,
                            schema=schema,
                        )
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
                except BudgetError:
                    raise
                except BaseException as exc:
                    last_error = exc
                    if attempt >= retry_count or not self._is_retryable(exc):
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
        raise ModelRuntimeError(code, metadata) from None

    def _prepare_budgeted_request(
        self,
        *,
        budget: ProviderBudgetContext,
        model: str,
        stage: str,
        agent_id: str,
        instructions: str,
        input_text: str,
        schema: type[StructuredT],
        max_output_tokens: int | None,
        reasoning_effort: ReasoningEffort,
    ) -> _BudgetedGenerationRequest:
        budget.ledger.manifest.resolve(model, "generation")
        output_bound = (
            self._max_output_tokens if max_output_tokens is None else max_output_tokens
        )
        if not 1 <= output_bound <= 1_200:
            raise BudgetLimitExceededError("generation_output_token_limit_exceeded")
        try:
            from openai.lib._parsing._responses import type_to_text_format_param
        except (AttributeError, ImportError) as exc:
            raise PricingManifestError(
                "provider_structured_output_adapter_unavailable"
            ) from exc
        text_format = type_to_text_format_param(schema)
        input_bound = generation_payload_token_bound(
            instructions=instructions,
            input_text=input_text,
            text_format=text_format,
        )
        budget.ledger.manifest.quote(
            model=model,
            operation="generation",
            input_token_bound=input_bound,
            output_token_bound=output_bound,
        )
        budget_client = self._retry_disabled_budget_client()
        self._assert_standard_endpoint(budget, budget_client)
        provider_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_text,
            "text": {"format": text_format},
            "max_output_tokens": output_bound,
            "reasoning": {"effort": reasoning_effort},
            "metadata": self._request_metadata(stage, agent_id),
            "safety_identifier": self._safety_identifier(),
            "service_tier": budget.ledger.manifest.service_tier,
            "store": False,
        }
        return _BudgetedGenerationRequest(
            provider_kwargs=provider_kwargs,
            input_token_bound=input_bound,
            output_token_bound=output_bound,
            fingerprint_sha256=request_fingerprint(provider_kwargs),
        )

    async def _budgeted_generation_attempt(
        self,
        *,
        budget: ProviderBudgetContext,
        call_id: str,
        attempt_number: int,
        request: _BudgetedGenerationRequest,
        schema: type[StructuredT],
    ) -> tuple[Any, StructuredT]:
        if budget.cancelled():
            raise BudgetCancelledError("provider_dispatch_cancelled")
        model = str(request.provider_kwargs["model"])
        reservation = budget.ledger.reserve_attempt(
            scope_id=budget.scope_id,
            call_id=call_id,
            attempt_number=attempt_number,
            operation="generation",
            purpose=budget.purpose,
            model=model,
            request_fingerprint_sha256=request.fingerprint_sha256,
            input_token_bound=request.input_token_bound,
            output_token_bound=request.output_token_bound,
            attempt_timeout_seconds=budget.attempt_timeout_seconds,
        )
        if not reservation.dispatch_allowed:
            raise BudgetDuplicateAttemptError("provider_attempt_already_reserved")
        if budget.cancelled():
            budget.ledger.abandon_undispatched(
                scope_id=budget.scope_id,
                attempt_id=reservation.attempt_id,
                error_code="provider_dispatch_cancelled",
            )
            raise BudgetCancelledError("provider_dispatch_cancelled")
        if not budget.ledger.mark_dispatched(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
        ):
            raise BudgetDuplicateAttemptError("provider_attempt_already_dispatched")
        remaining = seconds_until(reservation.deadline_at)
        timeout_seconds = min(budget.attempt_timeout_seconds, remaining)
        if timeout_seconds <= 0:
            budget.ledger.settle_unknown_usage(
                scope_id=budget.scope_id,
                attempt_id=reservation.attempt_id,
                result_status="timeout",
                error_code="model_timeout",
            )
            raise TimeoutError
        try:
            async with asyncio.timeout(timeout_seconds):
                budget_client = self._retry_disabled_budget_client()
                response = await budget_client.responses.create(
                    **request.provider_kwargs,
                    timeout=timeout_seconds,
                )
        except asyncio.CancelledError:
            budget.ledger.settle_unknown_usage(
                scope_id=budget.scope_id,
                attempt_id=reservation.attempt_id,
                result_status="cancelled",
                error_code="provider_attempt_cancelled",
            )
            raise
        except BaseException as exc:
            error_code = self._error_code(exc)
            budget.ledger.settle_unknown_usage(
                scope_id=budget.scope_id,
                attempt_id=reservation.attempt_id,
                result_status=("timeout" if error_code == "model_timeout" else "error"),
                error_code=error_code,
            )
            raise

        response_id = self._optional_response_text(response, "id")
        response_model = self._optional_response_text(response, "model")
        response_tier = self._optional_response_text(response, "service_tier")
        usage = generation_usage_from_response(response)
        if usage is None:
            if not self._response_pricing_identity_matches(
                budget,
                requested_model=model,
                response_model=response_model,
                response_service_tier=response_tier,
            ):
                budget.ledger.settle_unknown_usage(
                    scope_id=budget.scope_id,
                    attempt_id=reservation.attempt_id,
                    response_id=response_id,
                    response_model=response_model,
                    response_service_tier=response_tier,
                    result_status="error",
                    error_code="provider_pricing_identity_mismatch",
                )
                raise PricingManifestError(
                    "provider_response_pricing_identity_mismatch"
                )
            budget.ledger.settle_unknown_usage(
                scope_id=budget.scope_id,
                attempt_id=reservation.attempt_id,
                response_id=response_id,
                response_model=response_model,
                response_service_tier=response_tier,
            )
        else:
            budget.ledger.settle_known_usage(
                scope_id=budget.scope_id,
                attempt_id=reservation.attempt_id,
                usage=usage,
                response_id=response_id,
                response_model=response_model,
                response_service_tier=response_tier,
            )

        try:
            value = self._parse_budgeted_response(response, schema)
        except BaseException as exc:
            budget.ledger.record_attempt_result(
                scope_id=budget.scope_id,
                attempt_id=reservation.attempt_id,
                result_status="error",
                error_code=self._error_code(exc),
            )
            raise
        budget.ledger.record_attempt_result(
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            result_status="success",
        )
        return response, value

    def _retry_disabled_budget_client(self) -> Any:
        if self._budget_client is not None:
            return self._budget_client
        if getattr(self._client, "max_retries", None) == 0:
            self._budget_client = self._client
            return self._budget_client
        with_options = getattr(self._client, "with_options", None)
        if not callable(with_options):
            raise PricingManifestError("provider_sdk_retries_cannot_be_disabled")
        candidate = with_options(max_retries=0)
        if getattr(candidate, "max_retries", None) != 0:
            raise PricingManifestError("provider_sdk_retries_cannot_be_disabled")
        self._budget_client = candidate
        return candidate

    @staticmethod
    def _assert_standard_endpoint(
        budget: ProviderBudgetContext, budget_client: Any
    ) -> None:
        base_url = getattr(budget_client, "base_url", None)
        if base_url is None:
            return
        parsed = urlparse(str(base_url))
        if (
            parsed.scheme != "https"
            or parsed.hostname != budget.ledger.manifest.endpoint_host
        ):
            raise PricingManifestError("provider_endpoint_not_priced")

    @staticmethod
    def _response_pricing_identity_matches(
        budget: ProviderBudgetContext,
        *,
        requested_model: str,
        response_model: str | None,
        response_service_tier: str | None,
    ) -> bool:
        return (
            response_model is not None
            and response_service_tier == budget.ledger.manifest.service_tier
            and budget.ledger.manifest.response_model_matches(
                requested_model, response_model
            )
        )

    @staticmethod
    def _parse_budgeted_response(
        response: Any, schema: type[StructuredT]
    ) -> StructuredT:
        if getattr(response, "status", None) != "completed":
            raise _ModelResponseError("model_response_incomplete")
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text:
            raise _ModelResponseError("model_response_contract_violation")
        return schema.model_validate_json(output_text)

    @staticmethod
    def _optional_response_text(response: Any, field: str) -> str | None:
        value = getattr(response, field, None)
        return str(value) if value else None

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
        error_code: ModelErrorCode | None = None,
    ) -> ModelCallMetadata:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        input_details = getattr(usage, "input_tokens_details", None)
        cached_input_tokens = int(getattr(input_details, "cached_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        output_details = getattr(usage, "output_tokens_details", None)
        reasoning_tokens = int(getattr(output_details, "reasoning_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", input_tokens + output_tokens) or 0
        )
        raw_response_id = getattr(response, "id", None)
        return ModelCallMetadata(
            call_id=call_id,
            stage=stage,
            agent_id=agent_id,
            model=model,
            response_id=(str(raw_response_id) if raw_response_id else None),
            status=status,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
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
    def _error_code(error: BaseException | None) -> ModelErrorCode:
        if isinstance(error, (TimeoutError, APITimeoutError)):
            return "model_timeout"
        if isinstance(error, RateLimitError):
            return "model_rate_limited"
        if isinstance(error, APIConnectionError):
            return "model_connection_failed"
        if isinstance(error, APIStatusError):
            return "model_provider_error"
        if isinstance(error, _ModelResponseError):
            return error.code
        if isinstance(error, ValueError):
            return "model_response_invalid"
        return "model_runtime_failed"
