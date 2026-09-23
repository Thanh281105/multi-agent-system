"""Persist bounded turn runtime pins and counters independently of its result.

Revision ID: 20260909_0007
Revises: 20260909_0006
Create Date: 2026-09-09
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260909_0007"
down_revision: str | None = "20260909_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "v2_turns",
        sa.Column(
            "runtime_metadata",
            sa.JSON(none_as_null=True).with_variant(
                JSONB(none_as_null=True), "postgresql"
            ),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    # Losing pins/counters could make a replay misrepresent completed provider
    # work. Empty pre-P4 records are the only lossless downgrade case.
    rows = op.get_bind().execute(sa.text("SELECT runtime_metadata FROM v2_turns"))
    for (raw_metadata,) in rows:
        metadata = (
            json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
        )
        if metadata != {}:
            raise RuntimeError(
                "cannot downgrade turn runtime metadata: durable pins or counters "
                "would be lost"
            )
    op.drop_column("v2_turns", "runtime_metadata")
