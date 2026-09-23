"""Provider-neutral text embedding boundary for versioned vector indexes."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlparse

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from app.shared.budget import (
    BudgetCancelledError,
    BudgetDuplicateAttemptError,
    BudgetError,
    BudgetLimitExceededError,
    PricingManifestError,
    ProviderBudgetContext,
    conservative_text_token_bound,
    current_provider_budget,
    embedding_usage_from_response,
    request_fingerprint,
    seconds_until,
)


class EmbeddingRuntimeError(RuntimeError):
    """Raised when an embedding provider violates the local contract."""


class EmbeddingRuntime(Protocol):
    """Synchronous contract used inside MCP worker threads."""

    dimensions: int
    method: str

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingRuntime:
    """Validated OpenAI embeddings adapter preserving input order."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "text-embedding-3-small",
        dimensions: int = 1_536,
        timeout_seconds: float = 18.0,
        max_retries: int = 2,
        client: Any | None = None,
        budget_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not api_key.strip() and client is None:
            raise ValueError("OpenAIEmbeddingRuntime requires an API key")
        if not model.strip():
            raise ValueError("embedding model must not be blank")
        if not 32 <= dimensions <= 3_072:
            raise ValueError("embedding dimensions must be between 32 and 3072")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        self.model = model
        self.dimensions = dimensions
        self.method = f"openai_{model}_{dimensions}_v1"
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._api_key = api_key.strip()
        self._client = client or OpenAI(api_key=api_key, max_retries=max_retries)
        self._budget_client_factory = budget_client_factory

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        cleaned = [text.strip() for text in texts]
        if not cleaned or any(not text for text in cleaned):
            raise ValueError("embedding inputs must contain non-blank text")
        budget = current_provider_budget()
        if budget is not None:
            return self._embed_many_budgeted(cleaned, budget)
        try:
            response = self._client.embeddings.create(
                model=self.model,
                input=cleaned,
                dimensions=self.dimensions,
                encoding_format="float",
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            raise EmbeddingRuntimeError("embedding_provider_failed") from exc

        return self._validated_vectors(response, len(cleaned))

    def _embed_many_budgeted(
        self, cleaned: list[str], budget: ProviderBudgetContext
    ) -> list[list[float]]:
        self._assert_budgeted_request(cleaned, budget)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise EmbeddingRuntimeError(
                "budgeted_embedding_requires_sync_worker_thread"
            )
        return asyncio.run(self._embed_many_budgeted_async(cleaned, budget))

    async def _embed_many_budgeted_async(
        self, cleaned: list[str], budget: ProviderBudgetContext
    ) -> list[list[float]]:
        request: dict[str, Any] = {
            "model": self.model,
            "input": cleaned,
            "dimensions": self.dimensions,
            "encoding_format": "float",
        }
        fingerprint = request_fingerprint(request)
        input_bound = sum(conservative_text_token_bound(text) for text in cleaned)
        call_id = budget.new_call_id("embedding")
        retry_count = min(self._max_retries, budget.max_retries)
        last_error: BaseException | None = None
        if budget.cancelled():
            raise BudgetCancelledError("provider_dispatch_cancelled")
        budget_client = self._new_budget_client(budget)
        try:
            for attempt_index in range(retry_count + 1):
                if budget.cancelled():
                    raise BudgetCancelledError("provider_dispatch_cancelled")
                reservation = budget.ledger.reserve_attempt(
                    scope_id=budget.scope_id,
                    call_id=call_id,
                    attempt_number=attempt_index + 1,
                    operation="embedding",
                    purpose=budget.purpose,
                    model=self.model,
                    request_fingerprint_sha256=fingerprint,
                    input_token_bound=input_bound,
                    output_token_bound=0,
                    attempt_timeout_seconds=budget.attempt_timeout_seconds,
                )
                if not reservation.dispatch_allowed:
                    raise BudgetDuplicateAttemptError(
                        "provider_attempt_already_reserved"
                    )
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
                    raise BudgetDuplicateAttemptError(
                        "provider_attempt_already_dispatched"
                    )
                remaining = seconds_until(reservation.deadline_at)
                timeout_seconds = min(budget.attempt_timeout_seconds, remaining)
                if timeout_seconds <= 0:
                    budget.ledger.settle_unknown_usage(
                        scope_id=budget.scope_id,
                        attempt_id=reservation.attempt_id,
                        result_status="timeout",
                        error_code="embedding_timeout",
                    )
                    last_error = TimeoutError()
                else:
                    try:
                        async with asyncio.timeout(timeout_seconds):
                            response = await self._create_budgeted_embedding(
                                budget_client=budget_client,
                                budget=budget,
                                request=request,
                                timeout_seconds=timeout_seconds,
                            )
                    except (asyncio.CancelledError, BudgetCancelledError) as exc:
                        budget.ledger.settle_unknown_usage(
                            scope_id=budget.scope_id,
                            attempt_id=reservation.attempt_id,
                            result_status="cancelled",
                            error_code="provider_attempt_cancelled",
                        )
                        if isinstance(exc, BudgetCancelledError):
                            raise
                        raise BudgetCancelledError(
                            "provider_attempt_cancelled"
                        ) from exc
                    except BaseException as exc:
                        last_error = exc
                        error_code = self._error_code(exc)
                        budget.ledger.settle_unknown_usage(
                            scope_id=budget.scope_id,
                            attempt_id=reservation.attempt_id,
                            result_status=(
                                "timeout"
                                if error_code == "embedding_timeout"
                                else "error"
                            ),
                            error_code=error_code,
                        )
                    else:
                        return self._settle_and_validate_embedding(
                            response=response,
                            expected_count=len(cleaned),
                            budget=budget,
                            attempt_id=reservation.attempt_id,
                        )
                if attempt_index >= retry_count or not self._is_retryable(last_error):
                    break
                if budget.cancelled():
                    raise BudgetCancelledError("provider_dispatch_cancelled")
                remaining = seconds_until(reservation.deadline_at)
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.15 * (2**attempt_index), 1.2, remaining))
                if seconds_until(reservation.deadline_at) <= 0:
                    break
        finally:
            await self._close_budget_client(budget_client)

        if isinstance(last_error, BudgetError):
            raise last_error
        raise EmbeddingRuntimeError("embedding_provider_failed") from last_error

    async def _create_budgeted_embedding(
        self,
        *,
        budget_client: Any,
        budget: ProviderBudgetContext,
        request: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        create_task = asyncio.create_task(
            budget_client.embeddings.create(
                **request,
                timeout=timeout_seconds,
            )
        )
        try:
            while not create_task.done():
                if budget.cancelled():
                    create_task.cancel()
                    try:
                        await create_task
                    except asyncio.CancelledError:
                        pass
                    raise BudgetCancelledError("provider_dispatch_cancelled")
                await asyncio.wait({create_task}, timeout=0.05)
            return create_task.result()
        except BaseException:
            if not create_task.done():
                create_task.cancel()
                try:
                    await create_task
                except asyncio.CancelledError:
                    pass
            raise

    def _settle_and_validate_embedding(
        self,
        *,
        response: Any,
        expected_count: int,
        budget: ProviderBudgetContext,
        attempt_id: str,
    ) -> list[list[float]]:
        response_model = self._optional_response_text(response, "model")
        usage = embedding_usage_from_response(response)
        if usage is None:
            if response_model is None or not (
                budget.ledger.manifest.response_model_matches(
                    self.model, response_model
                )
            ):
                budget.ledger.settle_unknown_usage(
                    scope_id=budget.scope_id,
                    attempt_id=attempt_id,
                    response_model=response_model,
                    result_status="error",
                    error_code="provider_pricing_identity_mismatch",
                )
                raise PricingManifestError(
                    "provider_response_pricing_identity_mismatch"
                )
            budget.ledger.settle_unknown_usage(
                scope_id=budget.scope_id,
                attempt_id=attempt_id,
                response_model=response_model,
            )
        else:
            budget.ledger.settle_known_usage(
                scope_id=budget.scope_id,
                attempt_id=attempt_id,
                usage=usage,
                response_id=None,
                response_model=response_model,
                response_service_tier=None,
            )
        try:
            vectors = self._validated_vectors(response, expected_count)
        except BaseException as exc:
            budget.ledger.record_attempt_result(
                scope_id=budget.scope_id,
                attempt_id=attempt_id,
                result_status="error",
                error_code=self._validation_error_code(exc),
            )
            raise
        budget.ledger.record_attempt_result(
            scope_id=budget.scope_id,
            attempt_id=attempt_id,
            result_status="success",
        )
        return vectors

    def _assert_budgeted_request(
        self, cleaned: list[str], budget: ProviderBudgetContext
    ) -> None:
        pricing = budget.ledger.manifest.resolve(self.model, "embedding")
        if pricing.dimensions is not None and self.dimensions != pricing.dimensions:
            raise PricingManifestError("embedding_dimensions_not_priced")
        if len(cleaned) > (pricing.max_request_inputs or 0):
            raise BudgetLimitExceededError("embedding_request_input_limit_exceeded")
        bounds = [conservative_text_token_bound(text) for text in cleaned]
        if any(bound > (pricing.max_input_tokens or 0) for bound in bounds):
            raise BudgetLimitExceededError("embedding_input_token_limit_exceeded")
        total = sum(bounds)
        if total > (pricing.max_request_tokens or 0):
            raise BudgetLimitExceededError("embedding_request_token_limit_exceeded")
        budget.ledger.manifest.quote(
            model=self.model,
            operation="embedding",
            input_token_bound=total,
            output_token_bound=0,
        )

    def _new_budget_client(self, budget: ProviderBudgetContext) -> Any:
        if self._budget_client_factory is None:
            if not self._api_key:
                raise PricingManifestError("budgeted_embedding_requires_api_key")
            candidate = AsyncOpenAI(api_key=self._api_key, max_retries=0)
        else:
            candidate = self._budget_client_factory()
        if getattr(candidate, "max_retries", None) != 0:
            raise PricingManifestError("provider_sdk_retries_cannot_be_disabled")
        self._assert_standard_endpoint(budget, candidate)
        return candidate

    @staticmethod
    async def _close_budget_client(budget_client: Any) -> None:
        close = getattr(budget_client, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result

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
    def _optional_response_text(response: Any, field: str) -> str | None:
        value = getattr(response, field, None)
        return str(value) if value else None

    @staticmethod
    def _is_retryable(error: BaseException | None) -> bool:
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
    def _error_code(error: BaseException) -> str:
        if isinstance(error, (TimeoutError, APITimeoutError)):
            return "embedding_timeout"
        if isinstance(error, RateLimitError):
            return "embedding_rate_limited"
        if isinstance(error, APIConnectionError):
            return "embedding_connection_failed"
        if isinstance(error, APIStatusError):
            return "embedding_provider_error"
        return "embedding_provider_failed"

    @staticmethod
    def _validation_error_code(error: BaseException) -> str:
        message = str(error)
        for code in (
            "embedding_cardinality_mismatch",
            "embedding_order_mismatch",
            "embedding_dimension_mismatch",
            "embedding_contains_non_finite_value",
        ):
            if code in message:
                return code
        return "embedding_response_invalid"

    def _validated_vectors(
        self, response: Any, expected_count: int
    ) -> list[list[float]]:

        ordered = sorted(response.data, key=lambda item: int(item.index))
        if len(ordered) != expected_count:
            raise EmbeddingRuntimeError("embedding_cardinality_mismatch")
        if [int(item.index) for item in ordered] != list(range(expected_count)):
            raise EmbeddingRuntimeError("embedding_order_mismatch")

        vectors: list[list[float]] = []
        for item in ordered:
            vector = [float(value) for value in item.embedding]
            if len(vector) != self.dimensions:
                raise EmbeddingRuntimeError("embedding_dimension_mismatch")
            if any(not math.isfinite(value) for value in vector):
                raise EmbeddingRuntimeError("embedding_contains_non_finite_value")
            vectors.append(vector)
        return vectors
