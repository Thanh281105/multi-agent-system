"""Add Package 5 sandbox uniqueness and audit immutability guards.

Revision ID: 20260910_0008
Revises: 20260909_0007
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260910_0008"
down_revision: str | None = "20260909_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_CART_INDEX = "uq_v2_cart_active_owner"


def _create_active_cart_index() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_index(
            _ACTIVE_CART_INDEX,
            "v2_carts",
            ["tenant_id", "principal_id", "store_id"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        )
    elif dialect == "sqlite":
        # SQLite supports the same partial-index predicate and uses it for
        # local migration/schema parity in tests and development.
        op.create_index(
            _ACTIVE_CART_INDEX,
            "v2_carts",
            ["tenant_id", "principal_id", "store_id"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
        )


def _create_action_audit_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION v2_reject_immutable_action_audit_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'v2 action audit is immutable'
                USING ERRCODE = '23514';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_v2_action_audit_immutable
        BEFORE UPDATE OR DELETE ON v2_action_audit
        FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_action_audit_change()
        """
    )


def upgrade() -> None:
    _create_active_cart_index()
    _create_action_audit_guard()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_v2_action_audit_immutable ON v2_action_audit"
        )
        op.execute("DROP FUNCTION IF EXISTS v2_reject_immutable_action_audit_change()")
    dialect = op.get_bind().dialect.name
    if dialect in {"postgresql", "sqlite"}:
        op.drop_index(_ACTIVE_CART_INDEX, table_name="v2_carts")
