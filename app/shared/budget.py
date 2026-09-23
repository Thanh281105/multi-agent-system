"""Durable, exact provider-attempt budgeting shared by v2 workloads.

The scoped context is deliberately opt-in. Existing callers which do not enter
``provider_budget_scope`` continue to use the provider adapters unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.budget import (
    ProviderAttempt,
    ProviderBudgetAccount,
    ProviderBudgetScope,
)

BudgetPurpose = Literal[
    "chat", "ingestion", "benchmark", "warmup", "embedding", "judge"
]
ProviderOperation = Literal["generation", "embedding"]
AttemptResultStatus = Literal["success", "error", "timeout", "cancelled"]

NANO_USD_PER_USD = 1_000_000_000
DEFAULT_GLOBAL_LIMIT_NANO_USD = 100 * NANO_USD_PER_USD
DEFAULT_WARNING_NANO_USD = 50 * NANO_USD_PER_USD
DEFAULT_TURN_LIMIT_NANO_USD = NANO_USD_PER_USD // 4
DEFAULT_MAX_GENERATION_CALLS = 10
DEFAULT_MAX_PROVIDER_ATTEMPTS = 16
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_MAX_RETRIES = 1
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 18.0
DEFAULT_SCOPE_DEADLINE_SECONDS = 60.0
GENERATION_INPUT_TOKEN_LIMIT = 12_000
GENERATION_OUTPUT_TOKEN_LIMIT = 1_200
EMBEDDING_INPUT_TOKEN_LIMIT = 8_192
EMBEDDING_REQUEST_TOKEN_LIMIT = 300_000
EMBEDDING_REQUEST_INPUT_LIMIT = 2_048

_PURPOSES = frozenset(
    {"chat", "ingestion", "benchmark", "warmup", "embedding", "judge"}
)
_OPERATIONS = frozenset({"generation", "embedding"})
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}$")


class BudgetError(RuntimeError):
    """Base class for stable budget and accounting failures."""


class PricingManifestError(BudgetError):
    """The requested provider/model cannot be priced by the frozen manifest."""


class BudgetNotFoundError(BudgetError):
    """A required account, scope, or attempt does not exist."""


class BudgetConflictError(BudgetError):
    """An idempotency key was reused with different immutable data."""


class BudgetLimitExceededError(BudgetError):
    """A reservation would exceed a global or scoped cost ceiling."""


class BudgetAttemptLimitError(BudgetError):
    """A reservation would exceed a logical-call or provider-attempt limit."""


class BudgetConcurrencyError(BudgetError):
    """A scope already has the maximum number of active provider attempts."""


class BudgetDeadlineError(BudgetError):
    """The durable scope deadline passed before a new provider dispatch."""


class BudgetCancelledError(BudgetError):
    """The request was cancelled before a new provider dispatch."""


class BudgetDuplicateAttemptError(BudgetError):
    """A durable attempt key already exists and must not be dispatched again."""


class BudgetAccountingError(BudgetError):
    """Usage could not be recorded without violating ledger invariants."""


class BudgetReservationUnderestimatedError(BudgetAccountingError):
    """Known provider usage cost more than its conservative reservation."""


@dataclass(frozen=True, slots=True)
class ModelPricing:
    model: str
    operation: ProviderOperation
    input_nano_usd_per_token: int
    cached_input_nano_usd_per_token: int
    output_nano_usd_per_token: int
    dimensions: int | None
    max_input_tokens: int | None
    max_request_tokens: int | None
    max_request_inputs: int | None
    source_url: str
    limits_source_url: str | None
    verified_on: str


@dataclass(frozen=True, slots=True)
class PricingQuote:
    requested_model: str
    resolved_model: str
    operation: ProviderOperation
    input_token_bound: int
    output_token_bound: int
    reserved_nano_usd: int
    manifest_sha256: str
    pricing_profile: str


class PricingManifest:
    """Validated frozen prices and alias resolution for standard requests."""

    def __init__(
        self,
        *,
        manifest_sha256: str,
        pricing_profile: str,
        service_tier: str,
        endpoint_host: str,
        aliases: Mapping[str, str],
        models: Mapping[str, ModelPricing],
    ) -> None:
        self.manifest_sha256 = manifest_sha256
        self.pricing_profile = pricing_profile
        self.service_tier = service_tier
        self.endpoint_host = endpoint_host
        self._aliases = dict(aliases)
        self._models = dict(models)

    @classmethod
    def load(cls, path: str | Path) -> PricingManifest:
        raw = Path(path).read_bytes()
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PricingManifestError("pricing_manifest_invalid_json") from exc
        if not isinstance(document, dict):
            raise PricingManifestError("pricing_manifest_invalid_shape")
        canonical = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        cls._expect(document, "schema_version", "1.0")
        cls._expect(document, "currency", "USD")
        cls._expect(document, "price_unit", "usd_per_million_tokens")
        profile = cls._mapping(document.get("pricing_profile"), "pricing_profile")
        cls._expect(profile, "name", "standard_global_synchronous_text")
        cls._expect(profile, "service_tier", "default")
        cls._expect(profile, "endpoint_host", "api.openai.com")
        cls._expect(profile, "background", False)
        cls._expect(profile, "batch", False)
        cls._expect(profile, "regional_processing", False)
        aliases_raw = cls._mapping(document.get("aliases"), "aliases")
        models_raw = cls._mapping(document.get("models"), "models")
        aliases: dict[str, str] = {}
        for alias, target in aliases_raw.items():
            if not isinstance(alias, str) or not isinstance(target, str):
                raise PricingManifestError("pricing_manifest_invalid_alias")
            aliases[alias] = target
        models: dict[str, ModelPricing] = {}
        for model_name, value in models_raw.items():
            if not isinstance(model_name, str):
                raise PricingManifestError("pricing_manifest_invalid_model")
            entry = cls._mapping(value, f"models.{model_name}")
            operation = entry.get("operation")
            if operation not in _OPERATIONS:
                raise PricingManifestError("pricing_manifest_invalid_operation")
            models[model_name] = ModelPricing(
                model=model_name,
                operation=cast(ProviderOperation, operation),
                input_nano_usd_per_token=cls._price_rate(
                    entry.get("input_usd_per_million")
                ),
                cached_input_nano_usd_per_token=cls._price_rate(
                    entry.get("cached_input_usd_per_million")
                ),
                output_nano_usd_per_token=cls._price_rate(
                    entry.get("output_usd_per_million")
                ),
                dimensions=cls._optional_positive_int(entry.get("dimensions")),
                max_input_tokens=cls._optional_positive_int(
                    entry.get("max_input_tokens")
                ),
                max_request_tokens=cls._optional_positive_int(
                    entry.get("max_request_tokens")
                ),
                max_request_inputs=cls._optional_positive_int(
                    entry.get("max_request_inputs")
                ),
                source_url=cls._required_text(entry.get("source_url")),
                limits_source_url=cls._optional_text(entry.get("limits_source_url")),
                verified_on=cls._required_text(entry.get("verified_on")),
            )
        if not models:
            raise PricingManifestError("pricing_manifest_has_no_models")
        for target in aliases.values():
            if target not in models:
                raise PricingManifestError("pricing_manifest_alias_target_unknown")
        return cls(
            manifest_sha256=digest,
            pricing_profile=cast(str, profile["name"]),
            service_tier=cast(str, profile["service_tier"]),
            endpoint_host=cast(str, profile["endpoint_host"]),
            aliases=aliases,
            models=models,
        )

    def resolve(self, model: str, operation: ProviderOperation) -> ModelPricing:
        target = self._aliases.get(model, model)
        pricing = self._models.get(target)
        if pricing is None or pricing.operation != operation:
            raise PricingManifestError("provider_model_not_priced")
        return pricing

    def response_model_matches(self, requested_model: str, response_model: str) -> bool:
        requested_target = self._aliases.get(requested_model, requested_model)
        response_target = self._aliases.get(response_model, response_model)
        return requested_target == response_target and response_target in self._models

    def quote(
        self,
        *,
        model: str,
        operation: ProviderOperation,
        input_token_bound: int,
        output_token_bound: int,
    ) -> PricingQuote:
        if input_token_bound < 0 or output_token_bound < 0:
            raise ValueError("token bounds must be non-negative")
        pricing = self.resolve(model, operation)
        if operation == "generation" and (
            input_token_bound > GENERATION_INPUT_TOKEN_LIMIT
            or output_token_bound > GENERATION_OUTPUT_TOKEN_LIMIT
        ):
            raise BudgetLimitExceededError("generation_payload_limit_exceeded")
        if operation == "embedding":
            if output_token_bound != 0:
                raise BudgetLimitExceededError("embedding_output_must_be_zero")
            if (
                pricing.max_request_tokens is not None
                and input_token_bound > pricing.max_request_tokens
            ):
                raise BudgetLimitExceededError("embedding_request_token_limit_exceeded")
        reserved = (
            input_token_bound * pricing.input_nano_usd_per_token
            + output_token_bound * pricing.output_nano_usd_per_token
        )
        return PricingQuote(
            requested_model=model,
            resolved_model=pricing.model,
            operation=operation,
            input_token_bound=input_token_bound,
            output_token_bound=output_token_bound,
            reserved_nano_usd=reserved,
            manifest_sha256=self.manifest_sha256,
            pricing_profile=self.pricing_profile,
        )

    def known_cost(self, *, resolved_model: str, usage: ProviderUsage) -> int:
        pricing = self._models.get(resolved_model)
        if pricing is None:
            raise PricingManifestError("provider_model_not_priced")
        uncached = usage.input_tokens - usage.cached_input_tokens
        return (
            uncached * pricing.input_nano_usd_per_token
            + usage.cached_input_tokens * pricing.cached_input_nano_usd_per_token
            + usage.output_tokens * pricing.output_nano_usd_per_token
        )

    @staticmethod
    def _mapping(value: object, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PricingManifestError(f"pricing_manifest_invalid_{field}")
        return cast(dict[str, Any], value)

    @staticmethod
    def _expect(values: Mapping[str, Any], key: str, expected: object) -> None:
        if values.get(key) != expected:
            raise PricingManifestError(f"pricing_manifest_invalid_{key}")

    @staticmethod
    def _required_text(value: object) -> str:
        if not isinstance(value, str) or not value:
            raise PricingManifestError("pricing_manifest_invalid_text")
        return value

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        return PricingManifest._required_text(value)

    @staticmethod
    def _optional_positive_int(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise PricingManifestError("pricing_manifest_invalid_integer")
        return value

    @staticmethod
    def _price_rate(value: object) -> int:
        if not isinstance(value, str):
            raise PricingManifestError("pricing_manifest_price_must_be_string")
        try:
            nano_per_token = Decimal(value) * Decimal(1_000)
        except Exception as exc:
            raise PricingManifestError("pricing_manifest_invalid_price") from exc
        if nano_per_token < 0 or nano_per_token != nano_per_token.to_integral_value():
            raise PricingManifestError("pricing_manifest_price_not_exact_nanousd")
        return int(nano_per_token)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
            self.total_tokens,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError("usage token counts must be integers")
        if any(value < 0 for value in values):
            raise ValueError("usage token counts must be non-negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens must be included in output tokens")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total tokens must equal input plus output tokens")


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    attempt_id: str
    scope_id: str
    call_id: str
    attempt_number: int
    attempt_sequence: int
    logical_call_sequence: int | None
    quote: PricingQuote
    attempt_timeout_seconds: float
    deadline_at: datetime
    dispatch_allowed: bool
    duplicate: bool


@dataclass(frozen=True, slots=True)
class BudgetCostSummary:
    known_nano_usd: int
    reserved_nano_usd: int
    unknown_nano_usd: int
    hard_limit_nano_usd: int
    warning_threshold_nano_usd: int | None = None

    @property
    def encumbered_nano_usd(self) -> int:
        return self.known_nano_usd + self.reserved_nano_usd + self.unknown_nano_usd

    @property
    def known_usd(self) -> Decimal:
        return nano_usd_to_usd(self.known_nano_usd)

    @property
    def reserved_usd(self) -> Decimal:
        return nano_usd_to_usd(self.reserved_nano_usd)

    @property
    def unknown_usd(self) -> Decimal:
        return nano_usd_to_usd(self.unknown_nano_usd)

    @property
    def warning_reached(self) -> bool:
        return (
            self.warning_threshold_nano_usd is not None
            and self.encumbered_nano_usd >= self.warning_threshold_nano_usd
        )


@dataclass(frozen=True, slots=True)
class ScopeUsageSummary:
    """Authoritative retry-inclusive usage and accounting for one scope."""

    costs: BudgetCostSummary
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    provider_attempts: int
    pending_attempts: int
    unknown_cost_attempts: int
    attempts_without_usage: int


@dataclass(frozen=True, slots=True)
class ProviderAttemptSnapshot:
    """Immutable, read-only projection of one authoritative SQL attempt row."""

    attempt_id: str
    scope_id: str
    call_id: str
    attempt_number: int
    attempt_sequence: int
    logical_call_sequence: int | None
    operation: ProviderOperation
    purpose: BudgetPurpose
    provider: str
    requested_model: str
    resolved_model: str
    pricing_manifest_sha256: str
    pricing_profile: str
    request_fingerprint_sha256: str
    input_token_bound: int
    output_token_bound: int
    attempt_timeout_seconds: float
    reserved_nano_usd: int
    usage_status: Literal["reserved", "known", "unknown"]
    result_status: Literal["pending", "success", "error", "timeout", "cancelled"]
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    actual_cost_nano_usd: int | None
    response_id: str | None
    response_model: str | None
    response_service_tier: str | None
    error_code: str | None
    transport_started_at: datetime | None
    settled_at: datetime | None
    result_recorded_at: datetime | None
    created_at: datetime


class BudgetLedger(Protocol):
    manifest: PricingManifest

    def reserve_attempt(
        self,
        *,
        scope_id: str,
        call_id: str,
        attempt_number: int,
        operation: ProviderOperation,
        purpose: BudgetPurpose,
        model: str,
        request_fingerprint_sha256: str,
        input_token_bound: int,
        output_token_bound: int,
        attempt_timeout_seconds: float = DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    ) -> AttemptReservation: ...

    def mark_dispatched(self, *, scope_id: str, attempt_id: str) -> bool: ...

    def abandon_undispatched(
        self, *, scope_id: str, attempt_id: str, error_code: str
    ) -> None: ...

    def settle_known_usage(
        self,
        *,
        scope_id: str,
        attempt_id: str,
        usage: ProviderUsage,
        response_id: str | None,
        response_model: str | None,
        response_service_tier: str | None,
    ) -> None: ...

    def settle_unknown_usage(
        self,
        *,
        scope_id: str,
        attempt_id: str,
        response_id: str | None = None,
        response_model: str | None = None,
        response_service_tier: str | None = None,
        result_status: AttemptResultStatus | None = None,
        error_code: str | None = None,
    ) -> None: ...

    def record_attempt_result(
        self,
        *,
        scope_id: str,
        attempt_id: str,
        result_status: AttemptResultStatus,
        error_code: str | None = None,
    ) -> None: ...

    def recover_expired_attempts(self, *, scope_id: str) -> int: ...

    def scope_usage_summary(self, scope_id: str) -> ScopeUsageSummary: ...

    def scope_attempt_snapshots(
        self, scope_id: str
    ) -> tuple[ProviderAttemptSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class ProviderBudgetContext:
    """Context-local budget binding used by generation and embedding adapters."""

    ledger: BudgetLedger
    scope_id: str
    purpose: BudgetPurpose
    cancellation_requested: Callable[[], bool] | None = None
    call_id_factory: Callable[[ProviderOperation], str] | None = None
    max_retries: int = DEFAULT_MAX_RETRIES
    attempt_timeout_seconds: float = DEFAULT_ATTEMPT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _validate_identifier(self.scope_id, "scope_id")
        if self.purpose not in _PURPOSES:
            raise ValueError("unsupported budget purpose")
        if not 0 <= self.max_retries <= DEFAULT_MAX_RETRIES:
            raise ValueError("budgeted retries must be between 0 and 1")
        if not 0 < self.attempt_timeout_seconds <= DEFAULT_ATTEMPT_TIMEOUT_SECONDS:
            raise ValueError("budgeted attempt timeout must be at most 18 seconds")

    def cancelled(self) -> bool:
        return bool(
            self.cancellation_requested is not None and self.cancellation_requested()
        )

    def new_call_id(self, operation: ProviderOperation) -> str:
        if self.call_id_factory is not None:
            call_id = self.call_id_factory(operation)
        else:
            prefix = "mcall" if operation == "generation" else "ecall"
            call_id = f"{prefix}_{uuid4().hex}"
        _validate_identifier(call_id, "call_id")
        return call_id


_current_budget_context: ContextVar[ProviderBudgetContext | None] = ContextVar(
    "provider_budget_context", default=None
)


@contextmanager
def provider_budget_scope(
    context: ProviderBudgetContext,
) -> Iterator[ProviderBudgetContext]:
    """Bind a budget scope to only the current async/thread context."""

    token = _current_budget_context.set(context)
    try:
        yield context
    finally:
        _current_budget_context.reset(token)


def current_provider_budget() -> ProviderBudgetContext | None:
    return _current_budget_context.get()


class SQLProviderBudgetLedger:
    """Short-transaction SQLAlchemy implementation with fixed lock ordering."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        manifest: PricingManifest,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.manifest = manifest
        self._clock = clock or (lambda: datetime.now(UTC))

    def create_account(
        self,
        *,
        account_id: str,
        hard_limit_nano_usd: int = DEFAULT_GLOBAL_LIMIT_NANO_USD,
        warning_threshold_nano_usd: int = DEFAULT_WARNING_NANO_USD,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        _validate_identifier(account_id, "account_id")
        if not 0 < hard_limit_nano_usd <= DEFAULT_GLOBAL_LIMIT_NANO_USD:
            raise ValueError("global budget limit must be between zero and 100 USD")
        if not 0 <= warning_threshold_nano_usd <= hard_limit_nano_usd:
            raise ValueError("warning threshold must be within the global limit")
        if not 1 <= max_concurrency <= DEFAULT_MAX_CONCURRENCY:
            raise ValueError("global provider concurrency must be between one and two")
        with self._session_factory() as session, session.begin():
            existing = session.get(ProviderBudgetAccount, account_id)
            if existing is not None:
                expected = (
                    hard_limit_nano_usd,
                    warning_threshold_nano_usd,
                    max_concurrency,
                )
                actual = (
                    existing.hard_limit_nano_usd,
                    existing.warning_threshold_nano_usd,
                    existing.max_concurrency,
                )
                if actual != expected:
                    raise BudgetConflictError("budget_account_configuration_conflict")
                return
            session.add(
                ProviderBudgetAccount(
                    account_id=account_id,
                    currency="USD",
                    hard_limit_nano_usd=hard_limit_nano_usd,
                    warning_threshold_nano_usd=warning_threshold_nano_usd,
                    max_concurrency=max_concurrency,
                )
            )

    def create_scope(
        self,
        *,
        scope_id: str,
        account_id: str,
        purpose: BudgetPurpose,
        hard_limit_nano_usd: int = DEFAULT_TURN_LIMIT_NANO_USD,
        deadline_at: datetime | None = None,
        max_generation_calls: int = DEFAULT_MAX_GENERATION_CALLS,
        max_provider_attempts: int = DEFAULT_MAX_PROVIDER_ATTEMPTS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        _validate_identifier(scope_id, "scope_id")
        _validate_identifier(account_id, "account_id")
        if purpose not in _PURPOSES:
            raise ValueError("unsupported budget purpose")
        if hard_limit_nano_usd < 1:
            raise ValueError("scope budget limit must be positive")
        if purpose == "chat" and hard_limit_nano_usd > DEFAULT_TURN_LIMIT_NANO_USD:
            raise ValueError("chat scope budget cannot exceed 0.25 USD")
        if not 0 <= max_generation_calls <= DEFAULT_MAX_GENERATION_CALLS:
            raise ValueError("generation call limit must be between zero and 10")
        if not 1 <= max_provider_attempts <= DEFAULT_MAX_PROVIDER_ATTEMPTS:
            raise ValueError("provider attempt limit must be between one and 16")
        if not 1 <= max_concurrency <= DEFAULT_MAX_CONCURRENCY:
            raise ValueError("provider concurrency must be between one and two")
        now = self._now()
        if deadline_at is not None:
            _require_aware(deadline_at)
            if (
                not now
                < deadline_at
                <= now + timedelta(seconds=DEFAULT_SCOPE_DEADLINE_SECONDS)
            ):
                raise ValueError("budget scope deadline must be within 60 seconds")
        deadline = deadline_at or (
            now + timedelta(seconds=DEFAULT_SCOPE_DEADLINE_SECONDS)
        )
        with self._session_factory() as session, session.begin():
            account = session.get(ProviderBudgetAccount, account_id)
            if account is None:
                raise BudgetNotFoundError("budget_account_not_found")
            if hard_limit_nano_usd > account.hard_limit_nano_usd:
                raise ValueError("scope budget cannot exceed its global account limit")
            existing = session.get(ProviderBudgetScope, scope_id)
            if existing is not None:
                expected = (
                    account_id,
                    purpose,
                    hard_limit_nano_usd,
                    max_generation_calls,
                    max_provider_attempts,
                    max_concurrency,
                )
                actual = (
                    existing.account_id,
                    existing.purpose,
                    existing.hard_limit_nano_usd,
                    existing.max_generation_calls,
                    existing.max_provider_attempts,
                    existing.max_concurrency,
                )
                if actual != expected or (
                    deadline_at is not None
                    and _as_aware(existing.deadline_at) != deadline_at.astimezone(UTC)
                ):
                    raise BudgetConflictError("budget_scope_configuration_conflict")
                return
            session.add(
                ProviderBudgetScope(
                    scope_id=scope_id,
                    account_id=account_id,
                    purpose=purpose,
                    hard_limit_nano_usd=hard_limit_nano_usd,
                    deadline_at=deadline,
                    max_generation_calls=max_generation_calls,
                    max_provider_attempts=max_provider_attempts,
                    max_concurrency=max_concurrency,
                )
            )

    def reserve_attempt(
        self,
        *,
        scope_id: str,
        call_id: str,
        attempt_number: int,
        operation: ProviderOperation,
        purpose: BudgetPurpose,
        model: str,
        request_fingerprint_sha256: str,
        input_token_bound: int,
        output_token_bound: int,
        attempt_timeout_seconds: float = DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    ) -> AttemptReservation:
        _validate_identifier(scope_id, "scope_id")
        _validate_identifier(call_id, "call_id")
        if attempt_number not in (1, 2):
            raise BudgetAttemptLimitError("provider_retry_limit_exceeded")
        if operation not in _OPERATIONS:
            raise ValueError("unsupported provider operation")
        if purpose not in _PURPOSES:
            raise ValueError("unsupported budget purpose")
        if not _SHA256.fullmatch(request_fingerprint_sha256):
            raise ValueError("request fingerprint must be a lowercase SHA-256")
        timeout_ms = _timeout_milliseconds(attempt_timeout_seconds)
        quote = self.manifest.quote(
            model=model,
            operation=operation,
            input_token_bound=input_token_bound,
            output_token_bound=output_token_bound,
        )
        with self._session_factory() as session, session.begin():
            account_id = session.scalar(
                select(ProviderBudgetScope.account_id).where(
                    ProviderBudgetScope.scope_id == scope_id
                )
            )
            if account_id is None:
                raise BudgetNotFoundError("budget_scope_not_found")
            account = self._locked_one(
                session,
                select(ProviderBudgetAccount).where(
                    ProviderBudgetAccount.account_id == account_id
                ),
                "budget_account_not_found",
            )
            scope = self._locked_one(
                session,
                select(ProviderBudgetScope).where(
                    ProviderBudgetScope.scope_id == scope_id
                ),
                "budget_scope_not_found",
            )
            now = self._now()
            if purpose != scope.purpose:
                raise BudgetConflictError("provider_attempt_purpose_scope_mismatch")
            existing = session.scalar(
                select(ProviderAttempt).where(
                    ProviderAttempt.scope_id == scope_id,
                    ProviderAttempt.call_id == call_id,
                    ProviderAttempt.attempt_number == attempt_number,
                )
            )
            if existing is not None:
                self._assert_duplicate_matches(
                    existing,
                    operation=operation,
                    purpose=purpose,
                    quote=quote,
                    fingerprint=request_fingerprint_sha256,
                    timeout_ms=timeout_ms,
                )
                return self._reservation(existing, scope, duplicate=True)
            if _as_aware(scope.deadline_at) <= now:
                raise BudgetDeadlineError("budget_scope_deadline_exceeded")
            previous = list(
                session.scalars(
                    select(ProviderAttempt)
                    .where(
                        ProviderAttempt.scope_id == scope_id,
                        ProviderAttempt.call_id == call_id,
                    )
                    .order_by(ProviderAttempt.attempt_number)
                )
            )
            self._validate_attempt_sequence(
                previous,
                attempt_number=attempt_number,
                operation=operation,
                purpose=purpose,
                quote=quote,
                fingerprint=request_fingerprint_sha256,
                timeout_ms=timeout_ms,
            )
            is_new_generation_call = operation == "generation" and not previous
            if is_new_generation_call and (
                scope.generation_calls >= scope.max_generation_calls
            ):
                raise BudgetAttemptLimitError("generation_call_limit_exceeded")
            if scope.provider_attempts >= scope.max_provider_attempts:
                raise BudgetAttemptLimitError("provider_attempt_limit_exceeded")
            if scope.active_attempts >= scope.max_concurrency:
                raise BudgetConcurrencyError("provider_concurrency_limit_exceeded")
            if account.active_attempts >= account.max_concurrency:
                raise BudgetConcurrencyError(
                    "global_provider_concurrency_limit_exceeded"
                )
            if self._encumbered(account) + quote.reserved_nano_usd > (
                account.hard_limit_nano_usd
            ):
                raise BudgetLimitExceededError("global_budget_limit_exceeded")
            if self._encumbered(scope) + quote.reserved_nano_usd > (
                scope.hard_limit_nano_usd
            ):
                raise BudgetLimitExceededError("scope_budget_limit_exceeded")

            scope.provider_attempts += 1
            scope.active_attempts += 1
            account.active_attempts += 1
            logical_sequence: int | None = None
            if is_new_generation_call:
                scope.generation_calls += 1
                logical_sequence = scope.generation_calls
            elif previous:
                logical_sequence = previous[0].logical_call_sequence
            account.reserved_cost_nano_usd += quote.reserved_nano_usd
            scope.reserved_cost_nano_usd += quote.reserved_nano_usd
            account.updated_at = now
            scope.updated_at = now
            attempt = ProviderAttempt(
                attempt_id=f"pattempt_{uuid4().hex}",
                scope_id=scope_id,
                call_id=call_id,
                attempt_number=attempt_number,
                attempt_sequence=scope.provider_attempts,
                logical_call_sequence=logical_sequence,
                operation=operation,
                purpose=purpose,
                requested_model=model,
                resolved_model=quote.resolved_model,
                pricing_manifest_sha256=quote.manifest_sha256,
                pricing_profile=quote.pricing_profile,
                request_fingerprint_sha256=request_fingerprint_sha256,
                input_token_bound=input_token_bound,
                output_token_bound=output_token_bound,
                attempt_timeout_ms=timeout_ms,
                reserved_nano_usd=quote.reserved_nano_usd,
                created_at=now,
                updated_at=now,
            )
            session.add(attempt)
            session.flush()
            return self._reservation(attempt, scope, duplicate=False)

    def mark_dispatched(self, *, scope_id: str, attempt_id: str) -> bool:
        deadline_expired = False
        with self._session_factory() as session, session.begin():
            account, scope, attempt = self._lock_attempt_graph(
                session, scope_id, attempt_id
            )
            now = self._now()
            if attempt.usage_status != "reserved":
                return False
            if attempt.transport_started_at is not None:
                return False
            if _as_aware(scope.deadline_at) <= now:
                self._retire_undispatched(account, scope, attempt, now=now)
                deadline_expired = True
            else:
                attempt.transport_started_at = now
                attempt.updated_at = now
        if deadline_expired:
            raise BudgetDeadlineError("budget_scope_deadline_exceeded")
        return True

    def abandon_undispatched(
        self, *, scope_id: str, attempt_id: str, error_code: str
    ) -> None:
        _validate_optional_text(error_code, 96, "error_code")
        with self._session_factory() as session, session.begin():
            account, scope, attempt = self._lock_attempt_graph(
                session, scope_id, attempt_id
            )
            now = self._now()
            if attempt.usage_status == "known" and (
                attempt.transport_started_at is None
                and attempt.actual_cost_nano_usd == 0
                and attempt.result_status == "cancelled"
                and attempt.error_code == error_code
            ):
                return
            if attempt.usage_status != "reserved":
                raise BudgetConflictError("attempt_already_settled")
            if attempt.transport_started_at is not None:
                raise BudgetAccountingError("dispatched_attempt_cannot_be_abandoned")
            self._retire_undispatched(
                account,
                scope,
                attempt,
                now=now,
                result_status="cancelled",
                error_code=error_code,
            )

    def settle_known_usage(
        self,
        *,
        scope_id: str,
        attempt_id: str,
        usage: ProviderUsage,
        response_id: str | None,
        response_model: str | None,
        response_service_tier: str | None,
    ) -> None:
        _validate_optional_text(response_id, 256, "response_id")
        _validate_optional_text(response_model, 128, "response_model")
        _validate_optional_text(response_service_tier, 32, "response_service_tier")
        underestimated = False
        pricing_mismatch = False
        with self._session_factory() as session, session.begin():
            account, scope, attempt = self._lock_attempt_graph(
                session, scope_id, attempt_id
            )
            now = self._now()
            if attempt.usage_status == "known":
                known_expected = (
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_tokens,
                    usage.total_tokens,
                    response_id,
                    response_model,
                    response_service_tier,
                )
                known_actual = (
                    attempt.input_tokens,
                    attempt.cached_input_tokens,
                    attempt.output_tokens,
                    attempt.reasoning_tokens,
                    attempt.total_tokens,
                    attempt.response_id,
                    attempt.response_model,
                    attempt.response_service_tier,
                )
                if known_actual != known_expected:
                    raise BudgetConflictError("attempt_known_settlement_conflict")
                return
            if attempt.usage_status == "unknown":
                if not self._pricing_identity_matches(
                    attempt,
                    response_model=response_model,
                    response_service_tier=response_service_tier,
                ):
                    raise PricingManifestError(
                        "provider_response_pricing_identity_mismatch"
                    )
                self._assert_late_usage_consistent(
                    attempt,
                    usage=usage,
                    response_id=response_id,
                    response_model=response_model,
                    response_service_tier=response_service_tier,
                )
                actual_cost = self.manifest.known_cost(
                    resolved_model=attempt.resolved_model,
                    usage=usage,
                )
                self._move_unknown_to_known(
                    account,
                    scope,
                    reserved=attempt.reserved_nano_usd,
                    known=actual_cost,
                )
                attempt.usage_status = "known"
                attempt.input_tokens = usage.input_tokens
                attempt.cached_input_tokens = usage.cached_input_tokens
                attempt.output_tokens = usage.output_tokens
                attempt.reasoning_tokens = usage.reasoning_tokens
                attempt.total_tokens = usage.total_tokens
                attempt.actual_cost_nano_usd = actual_cost
                attempt.response_id = response_id
                attempt.response_model = response_model
                attempt.response_service_tier = response_service_tier
                attempt.settled_at = now
                attempt.updated_at = now
                account.updated_at = now
                scope.updated_at = now
                underestimated = actual_cost > attempt.reserved_nano_usd
            elif attempt.usage_status != "reserved":
                raise BudgetConflictError("attempt_usage_state_invalid")
            elif attempt.transport_started_at is None:
                raise BudgetAccountingError("attempt_was_not_dispatched")
            elif not self._pricing_identity_matches(
                attempt,
                response_model=response_model,
                response_service_tier=response_service_tier,
            ):
                self._move_reserved(
                    account,
                    scope,
                    reserved=attempt.reserved_nano_usd,
                    known=0,
                    unknown=attempt.reserved_nano_usd,
                )
                attempt.usage_status = "unknown"
                attempt.input_tokens = usage.input_tokens
                attempt.cached_input_tokens = usage.cached_input_tokens
                attempt.output_tokens = usage.output_tokens
                attempt.reasoning_tokens = usage.reasoning_tokens
                attempt.total_tokens = usage.total_tokens
                attempt.response_id = response_id
                attempt.response_model = response_model
                attempt.response_service_tier = response_service_tier
                attempt.result_status = "error"
                attempt.error_code = "provider_pricing_identity_mismatch"
                attempt.settled_at = now
                attempt.result_recorded_at = now
                attempt.updated_at = now
                account.updated_at = now
                scope.updated_at = now
                pricing_mismatch = True
            else:
                actual_cost = self.manifest.known_cost(
                    resolved_model=attempt.resolved_model,
                    usage=usage,
                )
                self._move_reserved(
                    account,
                    scope,
                    reserved=attempt.reserved_nano_usd,
                    known=actual_cost,
                    unknown=0,
                )
                attempt.usage_status = "known"
                attempt.input_tokens = usage.input_tokens
                attempt.cached_input_tokens = usage.cached_input_tokens
                attempt.output_tokens = usage.output_tokens
                attempt.reasoning_tokens = usage.reasoning_tokens
                attempt.total_tokens = usage.total_tokens
                attempt.actual_cost_nano_usd = actual_cost
                attempt.response_id = response_id
                attempt.response_model = response_model
                attempt.response_service_tier = response_service_tier
                attempt.settled_at = now
                attempt.updated_at = now
                account.updated_at = now
                scope.updated_at = now
                underestimated = actual_cost > attempt.reserved_nano_usd
                if underestimated:
                    attempt.result_status = "error"
                    attempt.error_code = "budget_reservation_underestimated"
                    attempt.result_recorded_at = now
        if pricing_mismatch:
            raise PricingManifestError("provider_response_pricing_identity_mismatch")
        if underestimated:
            raise BudgetReservationUnderestimatedError(
                "known_usage_exceeded_attempt_reservation"
            )

    def settle_unknown_usage(
        self,
        *,
        scope_id: str,
        attempt_id: str,
        response_id: str | None = None,
        response_model: str | None = None,
        response_service_tier: str | None = None,
        result_status: AttemptResultStatus | None = None,
        error_code: str | None = None,
    ) -> None:
        _validate_optional_text(response_id, 256, "response_id")
        _validate_optional_text(response_model, 128, "response_model")
        _validate_optional_text(response_service_tier, 32, "response_service_tier")
        self._validate_result(result_status, error_code, allow_none=True)
        with self._session_factory() as session, session.begin():
            account, scope, attempt = self._lock_attempt_graph(
                session, scope_id, attempt_id
            )
            now = self._now()
            if attempt.usage_status == "unknown":
                expected = (
                    response_id,
                    response_model,
                    response_service_tier,
                    result_status or attempt.result_status,
                    error_code,
                )
                actual = (
                    attempt.response_id,
                    attempt.response_model,
                    attempt.response_service_tier,
                    attempt.result_status,
                    attempt.error_code,
                )
                if actual != expected:
                    raise BudgetConflictError("attempt_unknown_settlement_conflict")
                return
            if attempt.usage_status != "reserved":
                raise BudgetConflictError("attempt_usage_already_known")
            if attempt.transport_started_at is None:
                raise BudgetAccountingError("attempt_was_not_dispatched")
            self._move_reserved(
                account,
                scope,
                reserved=attempt.reserved_nano_usd,
                known=0,
                unknown=attempt.reserved_nano_usd,
            )
            attempt.usage_status = "unknown"
            attempt.response_id = response_id
            attempt.response_model = response_model
            attempt.response_service_tier = response_service_tier
            attempt.settled_at = now
            if result_status is not None:
                attempt.result_status = result_status
                attempt.error_code = error_code
                attempt.result_recorded_at = now
            attempt.updated_at = now
            account.updated_at = now
            scope.updated_at = now

    def record_attempt_result(
        self,
        *,
        scope_id: str,
        attempt_id: str,
        result_status: AttemptResultStatus,
        error_code: str | None = None,
    ) -> None:
        self._validate_result(result_status, error_code, allow_none=False)
        with self._session_factory() as session, session.begin():
            attempt = self._locked_attempt(session, scope_id, attempt_id)
            now = self._now()
            if attempt.usage_status == "reserved":
                raise BudgetAccountingError("attempt_usage_must_settle_before_result")
            if attempt.result_status != "pending":
                if (attempt.result_status, attempt.error_code) != (
                    result_status,
                    error_code,
                ):
                    raise BudgetConflictError("attempt_result_conflict")
                return
            attempt.result_status = result_status
            attempt.error_code = error_code
            attempt.result_recorded_at = now
            attempt.updated_at = now

    def recover_expired_attempts(self, *, scope_id: str) -> int:
        """Retire crashed active attempts once their durable time bound passes."""

        recovered = 0
        with self._session_factory() as session, session.begin():
            account_id = session.scalar(
                select(ProviderBudgetScope.account_id).where(
                    ProviderBudgetScope.scope_id == scope_id
                )
            )
            if account_id is None:
                raise BudgetNotFoundError("budget_scope_not_found")
            account = self._locked_one(
                session,
                select(ProviderBudgetAccount).where(
                    ProviderBudgetAccount.account_id == account_id
                ),
                "budget_account_not_found",
            )
            scope = self._locked_one(
                session,
                select(ProviderBudgetScope).where(
                    ProviderBudgetScope.scope_id == scope_id
                ),
                "budget_scope_not_found",
            )
            now = self._now()
            active = list(
                session.scalars(
                    select(ProviderAttempt)
                    .where(
                        ProviderAttempt.scope_id == scope_id,
                        ProviderAttempt.usage_status == "reserved",
                    )
                    .order_by(ProviderAttempt.attempt_sequence)
                    .with_for_update()
                )
            )
            for attempt in active:
                scope_expired = _as_aware(scope.deadline_at) <= now
                if attempt.transport_started_at is None:
                    if not scope_expired:
                        continue
                    self._retire_undispatched(account, scope, attempt, now=now)
                    recovered += 1
                    continue
                attempt_expires = _as_aware(attempt.transport_started_at) + timedelta(
                    milliseconds=attempt.attempt_timeout_ms
                )
                if not scope_expired and attempt_expires > now:
                    continue
                self._move_reserved(
                    account,
                    scope,
                    reserved=attempt.reserved_nano_usd,
                    known=0,
                    unknown=attempt.reserved_nano_usd,
                )
                attempt.usage_status = "unknown"
                attempt.result_status = "timeout"
                attempt.error_code = "provider_attempt_recovered_after_deadline"
                attempt.settled_at = now
                attempt.result_recorded_at = now
                attempt.updated_at = now
                recovered += 1
            if recovered:
                account.updated_at = now
                scope.updated_at = now
        return recovered

    def account_summary(self, account_id: str) -> BudgetCostSummary:
        with self._session_factory() as session:
            account = session.get(ProviderBudgetAccount, account_id)
            if account is None:
                raise BudgetNotFoundError("budget_account_not_found")
            return BudgetCostSummary(
                known_nano_usd=account.known_cost_nano_usd,
                reserved_nano_usd=account.reserved_cost_nano_usd,
                unknown_nano_usd=account.unknown_cost_nano_usd,
                hard_limit_nano_usd=account.hard_limit_nano_usd,
                warning_threshold_nano_usd=account.warning_threshold_nano_usd,
            )

    def scope_summary(self, scope_id: str) -> BudgetCostSummary:
        with self._session_factory() as session:
            scope = session.get(ProviderBudgetScope, scope_id)
            if scope is None:
                raise BudgetNotFoundError("budget_scope_not_found")
            return BudgetCostSummary(
                known_nano_usd=scope.known_cost_nano_usd,
                reserved_nano_usd=scope.reserved_cost_nano_usd,
                unknown_nano_usd=scope.unknown_cost_nano_usd,
                hard_limit_nano_usd=scope.hard_limit_nano_usd,
            )

    def scope_usage_summary(self, scope_id: str) -> ScopeUsageSummary:
        with self._session_factory() as session:
            scope = session.get(ProviderBudgetScope, scope_id)
            if scope is None:
                raise BudgetNotFoundError("budget_scope_not_found")
            attempts = list(
                session.scalars(
                    select(ProviderAttempt).where(ProviderAttempt.scope_id == scope_id)
                )
            )
            costs = BudgetCostSummary(
                known_nano_usd=scope.known_cost_nano_usd,
                reserved_nano_usd=scope.reserved_cost_nano_usd,
                unknown_nano_usd=scope.unknown_cost_nano_usd,
                hard_limit_nano_usd=scope.hard_limit_nano_usd,
            )
            return ScopeUsageSummary(
                costs=costs,
                input_tokens=sum(item.input_tokens or 0 for item in attempts),
                cached_input_tokens=sum(
                    item.cached_input_tokens or 0 for item in attempts
                ),
                output_tokens=sum(item.output_tokens or 0 for item in attempts),
                reasoning_tokens=sum(item.reasoning_tokens or 0 for item in attempts),
                total_tokens=sum(item.total_tokens or 0 for item in attempts),
                provider_attempts=len(attempts),
                pending_attempts=sum(
                    item.usage_status == "reserved" for item in attempts
                ),
                unknown_cost_attempts=sum(
                    item.usage_status == "unknown" for item in attempts
                ),
                attempts_without_usage=sum(
                    item.input_tokens is None for item in attempts
                ),
            )

    def scope_attempt_snapshots(
        self, scope_id: str
    ) -> tuple[ProviderAttemptSnapshot, ...]:
        """Return authoritative attempt rows in their durable scope order."""

        with self._session_factory() as session:
            if session.get(ProviderBudgetScope, scope_id) is None:
                raise BudgetNotFoundError("budget_scope_not_found")
            attempts = tuple(
                session.scalars(
                    select(ProviderAttempt)
                    .where(ProviderAttempt.scope_id == scope_id)
                    .order_by(ProviderAttempt.attempt_sequence)
                )
            )
            return tuple(
                ProviderAttemptSnapshot(
                    attempt_id=item.attempt_id,
                    scope_id=item.scope_id,
                    call_id=item.call_id,
                    attempt_number=item.attempt_number,
                    attempt_sequence=item.attempt_sequence,
                    logical_call_sequence=item.logical_call_sequence,
                    operation=cast(ProviderOperation, item.operation),
                    purpose=cast(BudgetPurpose, item.purpose),
                    provider=item.provider,
                    requested_model=item.requested_model,
                    resolved_model=item.resolved_model,
                    pricing_manifest_sha256=item.pricing_manifest_sha256,
                    pricing_profile=item.pricing_profile,
                    request_fingerprint_sha256=item.request_fingerprint_sha256,
                    input_token_bound=item.input_token_bound,
                    output_token_bound=item.output_token_bound,
                    attempt_timeout_seconds=item.attempt_timeout_ms / 1_000,
                    reserved_nano_usd=item.reserved_nano_usd,
                    usage_status=cast(
                        Literal["reserved", "known", "unknown"],
                        item.usage_status,
                    ),
                    result_status=cast(
                        Literal["pending", "success", "error", "timeout", "cancelled"],
                        item.result_status,
                    ),
                    input_tokens=item.input_tokens,
                    cached_input_tokens=item.cached_input_tokens,
                    output_tokens=item.output_tokens,
                    reasoning_tokens=item.reasoning_tokens,
                    total_tokens=item.total_tokens,
                    actual_cost_nano_usd=item.actual_cost_nano_usd,
                    response_id=item.response_id,
                    response_model=item.response_model,
                    response_service_tier=item.response_service_tier,
                    error_code=item.error_code,
                    transport_started_at=(
                        _as_aware(item.transport_started_at)
                        if item.transport_started_at is not None
                        else None
                    ),
                    settled_at=(
                        _as_aware(item.settled_at)
                        if item.settled_at is not None
                        else None
                    ),
                    result_recorded_at=(
                        _as_aware(item.result_recorded_at)
                        if item.result_recorded_at is not None
                        else None
                    ),
                    created_at=_as_aware(item.created_at),
                )
                for item in attempts
            )

    def _lock_attempt_graph(
        self, session: Session, scope_id: str, attempt_id: str
    ) -> tuple[ProviderBudgetAccount, ProviderBudgetScope, ProviderAttempt]:
        account_id = session.scalar(
            select(ProviderBudgetScope.account_id).where(
                ProviderBudgetScope.scope_id == scope_id
            )
        )
        if account_id is None:
            raise BudgetNotFoundError("budget_scope_not_found")
        account = self._locked_one(
            session,
            select(ProviderBudgetAccount).where(
                ProviderBudgetAccount.account_id == account_id
            ),
            "budget_account_not_found",
        )
        scope = self._locked_one(
            session,
            select(ProviderBudgetScope).where(ProviderBudgetScope.scope_id == scope_id),
            "budget_scope_not_found",
        )
        attempt = self._locked_attempt(session, scope_id, attempt_id)
        return account, scope, attempt

    @staticmethod
    def _locked_one(
        session: Session,
        statement: Select[tuple[Any]],
        not_found_code: str,
    ) -> Any:
        value = session.scalar(statement.with_for_update())
        if value is None:
            raise BudgetNotFoundError(not_found_code)
        return value

    @staticmethod
    def _locked_attempt(
        session: Session, scope_id: str, attempt_id: str
    ) -> ProviderAttempt:
        attempt = session.scalar(
            select(ProviderAttempt)
            .where(
                ProviderAttempt.scope_id == scope_id,
                ProviderAttempt.attempt_id == attempt_id,
            )
            .with_for_update()
        )
        if attempt is None:
            raise BudgetNotFoundError("provider_attempt_not_found")
        return attempt

    @staticmethod
    def _encumbered(account_or_scope: Any) -> int:
        return int(
            account_or_scope.known_cost_nano_usd
            + account_or_scope.reserved_cost_nano_usd
            + account_or_scope.unknown_cost_nano_usd
        )

    @staticmethod
    def _move_reserved(
        account: ProviderBudgetAccount,
        scope: ProviderBudgetScope,
        *,
        reserved: int,
        known: int,
        unknown: int,
    ) -> None:
        if (
            account.reserved_cost_nano_usd < reserved
            or scope.reserved_cost_nano_usd < reserved
            or account.active_attempts < 1
            or scope.active_attempts < 1
        ):
            raise BudgetAccountingError("budget_bucket_invariant_failed")
        account.reserved_cost_nano_usd -= reserved
        scope.reserved_cost_nano_usd -= reserved
        account.known_cost_nano_usd += known
        scope.known_cost_nano_usd += known
        account.unknown_cost_nano_usd += unknown
        scope.unknown_cost_nano_usd += unknown
        account.active_attempts -= 1
        scope.active_attempts -= 1

    def _pricing_identity_matches(
        self,
        attempt: ProviderAttempt,
        *,
        response_model: str | None,
        response_service_tier: str | None,
    ) -> bool:
        if (
            attempt.pricing_manifest_sha256 != self.manifest.manifest_sha256
            or attempt.pricing_profile != self.manifest.pricing_profile
        ):
            return False
        try:
            current = self.manifest.resolve(
                attempt.requested_model,
                cast(ProviderOperation, attempt.operation),
            )
        except PricingManifestError:
            return False
        if current.model != attempt.resolved_model:
            return False
        if response_model is None or not self.manifest.response_model_matches(
            attempt.requested_model, response_model
        ):
            return False
        return response_service_tier in (None, self.manifest.service_tier) and (
            attempt.operation == "embedding"
            or response_service_tier == self.manifest.service_tier
        )

    @staticmethod
    def _assert_late_usage_consistent(
        attempt: ProviderAttempt,
        *,
        usage: ProviderUsage,
        response_id: str | None,
        response_model: str | None,
        response_service_tier: str | None,
    ) -> None:
        supplied_usage = (
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
            usage.total_tokens,
        )
        stored_usage = (
            attempt.input_tokens,
            attempt.cached_input_tokens,
            attempt.output_tokens,
            attempt.reasoning_tokens,
            attempt.total_tokens,
        )
        if any(value is not None for value in stored_usage) and (
            stored_usage != supplied_usage
        ):
            raise BudgetConflictError("late_provider_usage_conflict")
        for stored, supplied in (
            (attempt.response_id, response_id),
            (attempt.response_model, response_model),
            (attempt.response_service_tier, response_service_tier),
        ):
            if stored is not None and stored != supplied:
                raise BudgetConflictError("late_provider_response_identity_conflict")

    @staticmethod
    def _move_unknown_to_known(
        account: ProviderBudgetAccount,
        scope: ProviderBudgetScope,
        *,
        reserved: int,
        known: int,
    ) -> None:
        if (
            account.unknown_cost_nano_usd < reserved
            or scope.unknown_cost_nano_usd < reserved
        ):
            raise BudgetAccountingError("budget_unknown_bucket_invariant_failed")
        account.unknown_cost_nano_usd -= reserved
        scope.unknown_cost_nano_usd -= reserved
        account.known_cost_nano_usd += known
        scope.known_cost_nano_usd += known

    def _retire_undispatched(
        self,
        account: ProviderBudgetAccount,
        scope: ProviderBudgetScope,
        attempt: ProviderAttempt,
        *,
        now: datetime,
        result_status: Literal["timeout", "cancelled"] = "timeout",
        error_code: str = "budget_deadline_before_dispatch",
    ) -> None:
        self._move_reserved(
            account,
            scope,
            reserved=attempt.reserved_nano_usd,
            known=0,
            unknown=0,
        )
        attempt.usage_status = "known"
        attempt.input_tokens = 0
        attempt.cached_input_tokens = 0
        attempt.output_tokens = 0
        attempt.reasoning_tokens = 0
        attempt.total_tokens = 0
        attempt.actual_cost_nano_usd = 0
        attempt.result_status = result_status
        attempt.error_code = error_code
        attempt.settled_at = now
        attempt.result_recorded_at = now
        attempt.updated_at = now
        account.updated_at = now
        scope.updated_at = now

    @staticmethod
    def _validate_attempt_sequence(
        previous: list[ProviderAttempt],
        *,
        attempt_number: int,
        operation: ProviderOperation,
        purpose: BudgetPurpose,
        quote: PricingQuote,
        fingerprint: str,
        timeout_ms: int,
    ) -> None:
        if not previous:
            if attempt_number != 1:
                raise BudgetConflictError("first_provider_attempt_must_be_one")
            return
        first = previous[0]
        expected_number = previous[-1].attempt_number + 1
        if attempt_number != expected_number:
            raise BudgetConflictError("provider_attempt_sequence_conflict")
        if previous[-1].usage_status == "reserved":
            raise BudgetConflictError("previous_provider_attempt_still_active")
        immutable = (
            first.operation,
            first.purpose,
            first.requested_model,
            first.resolved_model,
            first.pricing_manifest_sha256,
            first.pricing_profile,
            first.request_fingerprint_sha256,
            first.attempt_timeout_ms,
        )
        candidate = (
            operation,
            purpose,
            quote.requested_model,
            quote.resolved_model,
            quote.manifest_sha256,
            quote.pricing_profile,
            fingerprint,
            timeout_ms,
        )
        if immutable != candidate:
            raise BudgetConflictError("provider_retry_payload_conflict")

    @staticmethod
    def _assert_duplicate_matches(
        existing: ProviderAttempt,
        *,
        operation: ProviderOperation,
        purpose: BudgetPurpose,
        quote: PricingQuote,
        fingerprint: str,
        timeout_ms: int,
    ) -> None:
        immutable = (
            existing.operation,
            existing.purpose,
            existing.requested_model,
            existing.resolved_model,
            existing.pricing_manifest_sha256,
            existing.pricing_profile,
            existing.request_fingerprint_sha256,
            existing.input_token_bound,
            existing.output_token_bound,
            existing.attempt_timeout_ms,
            existing.reserved_nano_usd,
        )
        candidate = (
            operation,
            purpose,
            quote.requested_model,
            quote.resolved_model,
            quote.manifest_sha256,
            quote.pricing_profile,
            fingerprint,
            quote.input_token_bound,
            quote.output_token_bound,
            timeout_ms,
            quote.reserved_nano_usd,
        )
        if immutable != candidate:
            raise BudgetConflictError("provider_attempt_key_conflict")

    @staticmethod
    def _reservation(
        attempt: ProviderAttempt,
        scope: ProviderBudgetScope,
        *,
        duplicate: bool,
    ) -> AttemptReservation:
        return AttemptReservation(
            attempt_id=attempt.attempt_id,
            scope_id=attempt.scope_id,
            call_id=attempt.call_id,
            attempt_number=attempt.attempt_number,
            attempt_sequence=attempt.attempt_sequence,
            logical_call_sequence=attempt.logical_call_sequence,
            quote=PricingQuote(
                requested_model=attempt.requested_model,
                resolved_model=attempt.resolved_model,
                operation=cast(ProviderOperation, attempt.operation),
                input_token_bound=attempt.input_token_bound,
                output_token_bound=attempt.output_token_bound,
                reserved_nano_usd=attempt.reserved_nano_usd,
                manifest_sha256=attempt.pricing_manifest_sha256,
                pricing_profile=attempt.pricing_profile,
            ),
            attempt_timeout_seconds=attempt.attempt_timeout_ms / 1_000,
            deadline_at=_as_aware(scope.deadline_at),
            dispatch_allowed=not duplicate,
            duplicate=duplicate,
        )

    @staticmethod
    def _validate_result(
        status: AttemptResultStatus | None,
        error_code: str | None,
        *,
        allow_none: bool,
    ) -> None:
        if status is None:
            if not allow_none or error_code is not None:
                raise ValueError("result status is required when recording an error")
            return
        if status == "success" and error_code is not None:
            raise ValueError("successful attempts cannot have an error code")
        if status != "success" and not error_code:
            raise ValueError("failed attempts require an error code")
        _validate_optional_text(error_code, 96, "error_code")

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now)
        return now.astimezone(UTC)


def usd_to_nano_usd(value: Decimal | str) -> int:
    decimal_value = value if isinstance(value, Decimal) else Decimal(value)
    nano = decimal_value * NANO_USD_PER_USD
    if nano < 0 or nano != nano.to_integral_value():
        raise ValueError("USD values must be non-negative and exact to nano-USD")
    return int(nano)


def nano_usd_to_usd(value: int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("nano-USD values must be non-negative integers")
    return Decimal(value) / Decimal(NANO_USD_PER_USD)


def conservative_text_token_bound(text: str) -> int:
    """Return a byte-level upper bound for byte-pair tokenizers.

    Each encoded token represents at least one source byte, so UTF-8 byte count
    is conservative without relying on the unsafe characters/4 heuristic.
    """

    return len(text.encode("utf-8"))


def generation_payload_token_bound(
    *, instructions: str, input_text: str, text_format: Mapping[str, Any]
) -> int:
    payload = {
        "instructions": instructions,
        "input": input_text,
        "text": {"format": text_format},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded)


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generation_usage_from_response(response: Any) -> ProviderUsage | None:
    usage = _field(response, "usage")
    if usage is None:
        return None
    return _safe_usage(
        input_tokens=_field(usage, "input_tokens"),
        cached_input_tokens=_field(
            _field(usage, "input_tokens_details"), "cached_tokens"
        ),
        output_tokens=_field(usage, "output_tokens"),
        reasoning_tokens=_field(
            _field(usage, "output_tokens_details"), "reasoning_tokens"
        ),
        total_tokens=_field(usage, "total_tokens"),
    )


def embedding_usage_from_response(response: Any) -> ProviderUsage | None:
    usage = _field(response, "usage")
    if usage is None:
        return None
    input_tokens = _field(usage, "prompt_tokens")
    total_tokens = _field(usage, "total_tokens")
    return _safe_usage(
        input_tokens=input_tokens,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        total_tokens=total_tokens,
    )


def seconds_until(deadline_at: datetime, *, now: datetime | None = None) -> float:
    current = now or datetime.now(UTC)
    _require_aware(current)
    return max(0.0, (_as_aware(deadline_at) - current).total_seconds())


def default_pricing_manifest_path() -> Path:
    return Path(__file__).with_name("pricing-manifest.json")


def _safe_usage(
    *,
    input_tokens: object,
    cached_input_tokens: object,
    output_tokens: object,
    reasoning_tokens: object,
    total_tokens: object,
) -> ProviderUsage | None:
    values = (
        input_tokens,
        cached_input_tokens,
        output_tokens,
        reasoning_tokens,
        total_tokens,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return None
    try:
        return ProviderUsage(
            input_tokens=cast(int, input_tokens),
            cached_input_tokens=cast(int, cached_input_tokens),
            output_tokens=cast(int, output_tokens),
            reasoning_tokens=cast(int, reasoning_tokens),
            total_tokens=cast(int, total_tokens),
        )
    except ValueError:
        return None


def _field(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} is not a valid durable identifier")


def _validate_optional_text(value: str | None, maximum: int, field: str) -> None:
    if value is not None and (not value or len(value) > maximum):
        raise ValueError(f"{field} must be non-empty and at most {maximum} characters")


def _timeout_milliseconds(value: float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("attempt timeout must be numeric")
    milliseconds = Decimal(str(value)) * Decimal(1_000)
    if (
        milliseconds < 1
        or milliseconds > int(DEFAULT_ATTEMPT_TIMEOUT_SECONDS * 1_000)
        or milliseconds != milliseconds.to_integral_value()
    ):
        raise ValueError("attempt timeout must be exact milliseconds up to 18 seconds")
    return int(milliseconds)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("budget timestamps must be timezone-aware")


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
