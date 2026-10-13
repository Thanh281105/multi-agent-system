"""Allow 30-second provider attempts for the frozen P7 policy.

Revision ID: 20261013_0009
Revises: 20260910_0008
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261013_0009"
down_revision: str | None = "20260910_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_v2_provider_attempt_reservation",
        "v2_provider_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_v2_provider_attempt_reservation",
        "v2_provider_attempts",
        "input_token_bound >= 0 AND output_token_bound >= 0 "
        "AND reserved_nano_usd >= 0 "
        "AND attempt_timeout_ms BETWEEN 1 AND 30000",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_v2_provider_attempt_reservation",
        "v2_provider_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_v2_provider_attempt_reservation",
        "v2_provider_attempts",
        "input_token_bound >= 0 AND output_token_bound >= 0 "
        "AND reserved_nano_usd >= 0 "
        "AND attempt_timeout_ms BETWEEN 1 AND 18000",
    )
