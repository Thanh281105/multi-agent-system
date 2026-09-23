"""Durable provider-attempt budget ledger models for v2 workloads."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now

_PURPOSE_CHECK = (
    "purpose IN ('chat', 'ingestion', 'benchmark', 'warmup', 'embedding', 'judge')"
)


class ProviderBudgetAccount(Base):
    """One shared provider account with exact nano-USD accounting buckets."""

    __tablename__ = "v2_budget_accounts"
    __table_args__ = (
        CheckConstraint("currency = 'USD'", name="ck_v2_budget_account_currency"),
        CheckConstraint("hard_limit_nano_usd > 0", name="ck_v2_budget_account_limit"),
        CheckConstraint(
            "hard_limit_nano_usd <= 100000000000",
            name="ck_v2_budget_account_limit_v2_max",
        ),
        CheckConstraint(
            "warning_threshold_nano_usd >= 0 AND "
            "warning_threshold_nano_usd <= hard_limit_nano_usd",
            name="ck_v2_budget_account_warning",
        ),
        CheckConstraint(
            "known_cost_nano_usd >= 0 AND reserved_cost_nano_usd >= 0 "
            "AND unknown_cost_nano_usd >= 0",
            name="ck_v2_budget_account_costs",
        ),
        CheckConstraint(
            "max_concurrency BETWEEN 1 AND 2",
            name="ck_v2_budget_account_concurrency_limit",
        ),
        CheckConstraint(
            "active_attempts >= 0 AND active_attempts <= max_concurrency",
            name="ck_v2_budget_account_active_count",
        ),
    )

    account_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD", server_default="USD"
    )
    hard_limit_nano_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    warning_threshold_nano_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    known_cost_nano_usd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reserved_cost_nano_usd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    unknown_cost_nano_usd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    active_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class ProviderBudgetScope(Base):
    """A turn or non-chat allocation sharing one provider budget account."""

    __tablename__ = "v2_budget_scopes"
    __table_args__ = (
        CheckConstraint(_PURPOSE_CHECK, name="ck_v2_budget_scope_purpose"),
        CheckConstraint("hard_limit_nano_usd > 0", name="ck_v2_budget_scope_limit"),
        CheckConstraint(
            "known_cost_nano_usd >= 0 AND reserved_cost_nano_usd >= 0 "
            "AND unknown_cost_nano_usd >= 0",
            name="ck_v2_budget_scope_costs",
        ),
        CheckConstraint(
            "max_generation_calls BETWEEN 0 AND 10",
            name="ck_v2_budget_scope_generation_limit",
        ),
        CheckConstraint(
            "max_provider_attempts BETWEEN 1 AND 16",
            name="ck_v2_budget_scope_attempt_limit",
        ),
        CheckConstraint(
            "max_concurrency BETWEEN 1 AND 2",
            name="ck_v2_budget_scope_concurrency_limit",
        ),
        CheckConstraint(
            "generation_calls >= 0 AND generation_calls <= max_generation_calls",
            name="ck_v2_budget_scope_generation_count",
        ),
        CheckConstraint(
            "provider_attempts >= 0 AND provider_attempts <= max_provider_attempts",
            name="ck_v2_budget_scope_attempt_count",
        ),
        CheckConstraint(
            "active_attempts >= 0 AND active_attempts <= max_concurrency",
            name="ck_v2_budget_scope_active_count",
        ),
        Index("ix_v2_budget_scopes_account_id", "account_id"),
    )

    scope_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("v2_budget_accounts.account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    hard_limit_nano_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    known_cost_nano_usd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reserved_cost_nano_usd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    unknown_cost_nano_usd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    max_generation_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    max_provider_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=16, server_default="16"
    )
    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    generation_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    provider_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    active_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class ProviderAttempt(Base):
    """One reserved provider transport attempt, including retry attempts."""

    __tablename__ = "v2_provider_attempts"
    __table_args__ = (
        UniqueConstraint(
            "scope_id",
            "call_id",
            "attempt_number",
            name="uq_v2_provider_attempt_key",
        ),
        CheckConstraint(
            "operation IN ('generation', 'embedding')",
            name="ck_v2_provider_attempt_operation",
        ),
        CheckConstraint(_PURPOSE_CHECK, name="ck_v2_provider_attempt_purpose"),
        CheckConstraint(
            "attempt_number BETWEEN 1 AND 2",
            name="ck_v2_provider_attempt_number",
        ),
        CheckConstraint(
            "attempt_sequence BETWEEN 1 AND 16",
            name="ck_v2_provider_attempt_sequence",
        ),
        CheckConstraint(
            "logical_call_sequence IS NULL OR logical_call_sequence BETWEEN 1 AND 10",
            name="ck_v2_provider_attempt_logical_sequence",
        ),
        CheckConstraint(
            "usage_status IN ('reserved', 'known', 'unknown')",
            name="ck_v2_provider_attempt_usage_status",
        ),
        CheckConstraint(
            "result_status IN ('pending', 'success', 'error', 'timeout', 'cancelled')",
            name="ck_v2_provider_attempt_result_status",
        ),
        CheckConstraint(
            "input_token_bound >= 0 AND output_token_bound >= 0 "
            "AND reserved_nano_usd >= 0 "
            "AND attempt_timeout_ms BETWEEN 1 AND 18000",
            name="ck_v2_provider_attempt_reservation",
        ),
        CheckConstraint(
            "actual_cost_nano_usd IS NULL OR actual_cost_nano_usd >= 0",
            name="ck_v2_provider_attempt_actual_cost",
        ),
        CheckConstraint(
            "(usage_status = 'known' AND input_tokens IS NOT NULL "
            "AND cached_input_tokens IS NOT NULL AND output_tokens IS NOT NULL "
            "AND reasoning_tokens IS NOT NULL AND total_tokens IS NOT NULL "
            "AND actual_cost_nano_usd IS NOT NULL) "
            "OR (usage_status = 'reserved' AND input_tokens IS NULL "
            "AND cached_input_tokens IS NULL AND output_tokens IS NULL "
            "AND reasoning_tokens IS NULL AND total_tokens IS NULL "
            "AND actual_cost_nano_usd IS NULL) "
            "OR (usage_status = 'unknown' AND actual_cost_nano_usd IS NULL "
            "AND ((input_tokens IS NULL AND cached_input_tokens IS NULL "
            "AND output_tokens IS NULL AND reasoning_tokens IS NULL "
            "AND total_tokens IS NULL) OR (input_tokens IS NOT NULL "
            "AND cached_input_tokens IS NOT NULL AND output_tokens IS NOT NULL "
            "AND reasoning_tokens IS NOT NULL AND total_tokens IS NOT NULL)))",
            name="ck_v2_provider_attempt_usage_shape",
        ),
        CheckConstraint(
            "input_tokens IS NULL OR (input_tokens >= 0 "
            "AND cached_input_tokens >= 0 AND output_tokens >= 0 "
            "AND reasoning_tokens >= 0 AND total_tokens >= 0 "
            "AND cached_input_tokens <= input_tokens "
            "AND reasoning_tokens <= output_tokens "
            "AND total_tokens = input_tokens + output_tokens)",
            name="ck_v2_provider_attempt_usage_values",
        ),
        CheckConstraint(
            "(result_status = 'pending' AND error_code IS NULL) "
            "OR (result_status = 'success' AND error_code IS NULL) "
            "OR (result_status IN ('error', 'timeout', 'cancelled') "
            "AND error_code IS NOT NULL)",
            name="ck_v2_provider_attempt_result_shape",
        ),
        CheckConstraint(
            "length(pricing_manifest_sha256) = 64 "
            "AND length(request_fingerprint_sha256) = 64",
            name="ck_v2_provider_attempt_hash_lengths",
        ),
        Index("ix_v2_provider_attempts_scope_created", "scope_id", "created_at"),
        Index("ix_v2_provider_attempts_call", "scope_id", "call_id"),
    )

    attempt_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    scope_id: Mapped[str] = mapped_column(
        ForeignKey("v2_budget_scopes.scope_id", ondelete="RESTRICT"),
        nullable=False,
    )
    call_id: Mapped[str] = mapped_column(String(96), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_call_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(24), nullable=False, default="openai", server_default="openai"
    )
    requested_model: Mapped[str] = mapped_column(String(128), nullable=False)
    resolved_model: Mapped[str] = mapped_column(String(128), nullable=False)
    pricing_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pricing_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_token_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    output_token_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_nano_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reserved", server_default="reserved"
    )
    result_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_cost_nano_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    response_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    response_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_service_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    transport_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
