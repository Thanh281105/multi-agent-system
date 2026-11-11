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
    _replace_timeout_constraint(30000)


def downgrade() -> None:
    # Refuse before SQLite's copy-and-move DDL can modify the source table.
    count = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT COUNT(*) FROM v2_provider_attempts WHERE attempt_timeout_ms > 18000"
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "cannot downgrade provider attempts with timeout above 18000 ms"
        )
    _replace_timeout_constraint(18000)


def _replace_timeout_constraint(maximum_ms: int) -> None:
    with op.batch_alter_table("v2_provider_attempts") as batch:
        batch.drop_constraint("ck_v2_provider_attempt_reservation", type_="check")
        batch.create_check_constraint(
            "ck_v2_provider_attempt_reservation",
            "input_token_bound >= 0 AND output_token_bound >= 0 "
            "AND reserved_nano_usd >= 0 "
            f"AND attempt_timeout_ms BETWEEN 1 AND {maximum_ms}",
        )
