"""Add the durable shared provider-attempt budget ledger.

Revision ID: 20260909_0005
Revises: 20260909_0004
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0005"
down_revision: str | None = "20260909_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSE_CHECK = (
    "purpose IN ('chat', 'ingestion', 'benchmark', 'warmup', 'embedding', 'judge')"
)


def upgrade() -> None:
    op.create_table(
        "v2_budget_accounts",
        sa.Column("account_id", sa.String(length=120), nullable=False),
        sa.Column(
            "currency", sa.String(length=3), server_default="USD", nullable=False
        ),
        sa.Column("hard_limit_nano_usd", sa.BigInteger(), nullable=False),
        sa.Column("warning_threshold_nano_usd", sa.BigInteger(), nullable=False),
        sa.Column(
            "known_cost_nano_usd", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "reserved_cost_nano_usd",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "unknown_cost_nano_usd",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("max_concurrency", sa.Integer(), server_default="2", nullable=False),
        sa.Column("active_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("currency = 'USD'", name="ck_v2_budget_account_currency"),
        sa.CheckConstraint(
            "hard_limit_nano_usd > 0", name="ck_v2_budget_account_limit"
        ),
        sa.CheckConstraint(
            "hard_limit_nano_usd <= 100000000000",
            name="ck_v2_budget_account_limit_v2_max",
        ),
        sa.CheckConstraint(
            "warning_threshold_nano_usd >= 0 AND "
            "warning_threshold_nano_usd <= hard_limit_nano_usd",
            name="ck_v2_budget_account_warning",
        ),
        sa.CheckConstraint(
            "known_cost_nano_usd >= 0 AND reserved_cost_nano_usd >= 0 "
            "AND unknown_cost_nano_usd >= 0",
            name="ck_v2_budget_account_costs",
        ),
        sa.CheckConstraint(
            "max_concurrency BETWEEN 1 AND 2",
            name="ck_v2_budget_account_concurrency_limit",
        ),
        sa.CheckConstraint(
            "active_attempts >= 0 AND active_attempts <= max_concurrency",
            name="ck_v2_budget_account_active_count",
        ),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_table(
        "v2_budget_scopes",
        sa.Column("scope_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=120), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("hard_limit_nano_usd", sa.BigInteger(), nullable=False),
        sa.Column(
            "known_cost_nano_usd", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "reserved_cost_nano_usd",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "unknown_cost_nano_usd",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "max_generation_calls", sa.Integer(), server_default="10", nullable=False
        ),
        sa.Column(
            "max_provider_attempts", sa.Integer(), server_default="16", nullable=False
        ),
        sa.Column("max_concurrency", sa.Integer(), server_default="2", nullable=False),
        sa.Column("generation_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "provider_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("active_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(_PURPOSE_CHECK, name="ck_v2_budget_scope_purpose"),
        sa.CheckConstraint("hard_limit_nano_usd > 0", name="ck_v2_budget_scope_limit"),
        sa.CheckConstraint(
            "known_cost_nano_usd >= 0 AND reserved_cost_nano_usd >= 0 "
            "AND unknown_cost_nano_usd >= 0",
            name="ck_v2_budget_scope_costs",
        ),
        sa.CheckConstraint(
            "max_generation_calls BETWEEN 0 AND 10",
            name="ck_v2_budget_scope_generation_limit",
        ),
        sa.CheckConstraint(
            "max_provider_attempts BETWEEN 1 AND 16",
            name="ck_v2_budget_scope_attempt_limit",
        ),
        sa.CheckConstraint(
            "max_concurrency BETWEEN 1 AND 2",
            name="ck_v2_budget_scope_concurrency_limit",
        ),
        sa.CheckConstraint(
            "generation_calls >= 0 AND generation_calls <= max_generation_calls",
            name="ck_v2_budget_scope_generation_count",
        ),
        sa.CheckConstraint(
            "provider_attempts >= 0 AND provider_attempts <= max_provider_attempts",
            name="ck_v2_budget_scope_attempt_count",
        ),
        sa.CheckConstraint(
            "active_attempts >= 0 AND active_attempts <= max_concurrency",
            name="ck_v2_budget_scope_active_count",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["v2_budget_accounts.account_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("scope_id"),
    )
    op.create_index(
        "ix_v2_budget_scopes_account_id",
        "v2_budget_scopes",
        ["account_id"],
        unique=False,
    )
    op.create_table(
        "v2_provider_attempts",
        sa.Column("attempt_id", sa.String(length=96), nullable=False),
        sa.Column("scope_id", sa.String(length=160), nullable=False),
        sa.Column("call_id", sa.String(length=96), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempt_sequence", sa.Integer(), nullable=False),
        sa.Column("logical_call_sequence", sa.Integer(), nullable=True),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column(
            "provider", sa.String(length=24), server_default="openai", nullable=False
        ),
        sa.Column("requested_model", sa.String(length=128), nullable=False),
        sa.Column("resolved_model", sa.String(length=128), nullable=False),
        sa.Column("pricing_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("pricing_profile", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_token_bound", sa.Integer(), nullable=False),
        sa.Column("output_token_bound", sa.Integer(), nullable=False),
        sa.Column("attempt_timeout_ms", sa.Integer(), nullable=False),
        sa.Column("reserved_nano_usd", sa.BigInteger(), nullable=False),
        sa.Column(
            "usage_status",
            sa.String(length=16),
            server_default="reserved",
            nullable=False,
        ),
        sa.Column(
            "result_status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("actual_cost_nano_usd", sa.BigInteger(), nullable=True),
        sa.Column("response_id", sa.String(length=256), nullable=True),
        sa.Column("response_model", sa.String(length=128), nullable=True),
        sa.Column("response_service_tier", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("transport_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation IN ('generation', 'embedding')",
            name="ck_v2_provider_attempt_operation",
        ),
        sa.CheckConstraint(_PURPOSE_CHECK, name="ck_v2_provider_attempt_purpose"),
        sa.CheckConstraint(
            "attempt_number BETWEEN 1 AND 2",
            name="ck_v2_provider_attempt_number",
        ),
        sa.CheckConstraint(
            "attempt_sequence BETWEEN 1 AND 16",
            name="ck_v2_provider_attempt_sequence",
        ),
        sa.CheckConstraint(
            "logical_call_sequence IS NULL OR logical_call_sequence BETWEEN 1 AND 10",
            name="ck_v2_provider_attempt_logical_sequence",
        ),
        sa.CheckConstraint(
            "usage_status IN ('reserved', 'known', 'unknown')",
            name="ck_v2_provider_attempt_usage_status",
        ),
        sa.CheckConstraint(
            "result_status IN ('pending', 'success', 'error', 'timeout', 'cancelled')",
            name="ck_v2_provider_attempt_result_status",
        ),
        sa.CheckConstraint(
            "input_token_bound >= 0 AND output_token_bound >= 0 "
            "AND reserved_nano_usd >= 0 "
            "AND attempt_timeout_ms BETWEEN 1 AND 18000",
            name="ck_v2_provider_attempt_reservation",
        ),
        sa.CheckConstraint(
            "actual_cost_nano_usd IS NULL OR actual_cost_nano_usd >= 0",
            name="ck_v2_provider_attempt_actual_cost",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "input_tokens IS NULL OR (input_tokens >= 0 "
            "AND cached_input_tokens >= 0 AND output_tokens >= 0 "
            "AND reasoning_tokens >= 0 AND total_tokens >= 0 "
            "AND cached_input_tokens <= input_tokens "
            "AND reasoning_tokens <= output_tokens "
            "AND total_tokens = input_tokens + output_tokens)",
            name="ck_v2_provider_attempt_usage_values",
        ),
        sa.CheckConstraint(
            "(result_status = 'pending' AND error_code IS NULL) "
            "OR (result_status = 'success' AND error_code IS NULL) "
            "OR (result_status IN ('error', 'timeout', 'cancelled') "
            "AND error_code IS NOT NULL)",
            name="ck_v2_provider_attempt_result_shape",
        ),
        sa.CheckConstraint(
            "length(pricing_manifest_sha256) = 64 "
            "AND length(request_fingerprint_sha256) = 64",
            name="ck_v2_provider_attempt_hash_lengths",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id"], ["v2_budget_scopes.scope_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "scope_id",
            "call_id",
            "attempt_number",
            name="uq_v2_provider_attempt_key",
        ),
    )
    op.create_index(
        "ix_v2_provider_attempts_scope_created",
        "v2_provider_attempts",
        ["scope_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_v2_provider_attempts_call",
        "v2_provider_attempts",
        ["scope_id", "call_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_v2_provider_attempts_call", table_name="v2_provider_attempts")
    op.drop_index(
        "ix_v2_provider_attempts_scope_created", table_name="v2_provider_attempts"
    )
    op.drop_table("v2_provider_attempts")
    op.drop_index("ix_v2_budget_scopes_account_id", table_name="v2_budget_scopes")
    op.drop_table("v2_budget_scopes")
    op.drop_table("v2_budget_accounts")
