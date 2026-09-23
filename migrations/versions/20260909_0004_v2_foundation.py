"""Add the durable v2 conversation, sandbox, and knowledge foundation.

Revision ID: 20260909_0004
Revises: 20260830_0003
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_0004"
down_revision: str | None = "20260830_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VALUE = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)
OWNER_NAMES = ("tenant_id", "principal_id", "mode", "store_id")


def _id() -> sa.Column[str]:
    return sa.Column("id", sa.String(length=64), nullable=False)


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _owner_columns() -> list[sa.Column[str]]:
    return [
        sa.Column("tenant_id", sa.String(length=160), nullable=False),
        sa.Column("principal_id", sa.String(length=160), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
    ]


def upgrade() -> None:
    _create_knowledge_tables()
    _create_conversation_tables()
    _create_sandbox_tables()
    _create_postgresql_immutability_guards()


def _create_knowledge_tables() -> None:
    op.create_table(
        "v2_knowledge_corpus_versions",
        _id(),
        sa.Column("corpus_name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "manifest", JSON_VALUE, server_default=sa.text("'{}'"), nullable=False
        ),
        _created_at(),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_v2_corpus_versions_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("corpus_name", "version", name="uq_v2_corpus_name_version"),
    )
    op.create_table(
        "v2_knowledge_index_manifests",
        _id(),
        sa.Column("corpus_version_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=160), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.String(length=120), nullable=False),
        sa.Column("enrichment_policy_version", sa.String(length=120), nullable=False),
        sa.Column(
            "manifest", JSON_VALUE, server_default=sa.text("'{}'"), nullable=False
        ),
        _created_at(),
        sa.CheckConstraint(
            "embedding_dimension > 0", name="ck_v2_index_dimension_positive"
        ),
        sa.ForeignKeyConstraint(
            ["corpus_version_id"],
            ["v2_knowledge_corpus_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "corpus_version_id", "fingerprint", name="uq_v2_index_corpus_fingerprint"
        ),
        sa.UniqueConstraint("id", "corpus_version_id", name="uq_v2_index_id_corpus"),
    )
    op.create_index(
        "ix_v2_knowledge_index_manifests_corpus_version_id",
        "v2_knowledge_index_manifests",
        ["corpus_version_id"],
    )
    op.create_table(
        "v2_knowledge_documents",
        _id(),
        sa.Column("corpus_version_id", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2_000), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("use_basis", sa.String(length=500), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("document_version", sa.String(length=120), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "access_metadata",
            JSON_VALUE,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "source_metadata",
            JSON_VALUE,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["corpus_version_id"],
            ["v2_knowledge_corpus_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "corpus_version_id",
            "source_url",
            "document_version",
            name="uq_v2_document_source_version",
        ),
        sa.UniqueConstraint("id", "corpus_version_id", name="uq_v2_document_id_corpus"),
    )
    op.create_index(
        "ix_v2_knowledge_documents_corpus_version_id",
        "v2_knowledge_documents",
        ["corpus_version_id"],
    )
    op.create_table(
        "v2_knowledge_chunks",
        _id(),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("corpus_version_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "metadata", JSON_VALUE, server_default=sa.text("'{}'"), nullable=False
        ),
        _created_at(),
        sa.CheckConstraint("chunk_index >= 0", name="ck_v2_chunk_index_non_negative"),
        sa.CheckConstraint("token_count >= 0", name="ck_v2_chunk_tokens_non_negative"),
        sa.ForeignKeyConstraint(
            ["document_id", "corpus_version_id"],
            ["v2_knowledge_documents.id", "v2_knowledge_documents.corpus_version_id"],
            name="fk_v2_chunk_document_corpus",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name="uq_v2_chunk_document_index"
        ),
        sa.UniqueConstraint("id", "corpus_version_id", name="uq_v2_chunk_id_corpus"),
    )
    op.create_index(
        "ix_v2_knowledge_chunks_corpus_version_id",
        "v2_knowledge_chunks",
        ["corpus_version_id"],
    )
    op.create_table(
        "v2_knowledge_vectors",
        _id(),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("index_manifest_id", sa.String(length=64), nullable=False),
        sa.Column("corpus_version_id", sa.String(length=64), nullable=False),
        sa.Column("vector", JSON_VALUE, nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["chunk_id", "corpus_version_id"],
            ["v2_knowledge_chunks.id", "v2_knowledge_chunks.corpus_version_id"],
            name="fk_v2_vector_chunk_corpus",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["index_manifest_id", "corpus_version_id"],
            [
                "v2_knowledge_index_manifests.id",
                "v2_knowledge_index_manifests.corpus_version_id",
            ],
            name="fk_v2_vector_index_corpus",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chunk_id", "index_manifest_id", name="uq_v2_vector_chunk_index"
        ),
    )
    op.create_index(
        "ix_v2_knowledge_vectors_corpus_version_id",
        "v2_knowledge_vectors",
        ["corpus_version_id"],
    )
    op.create_table(
        "v2_book_mappings",
        _id(),
        sa.Column("corpus_version_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("mapping_status", sa.String(length=24), nullable=False),
        sa.Column("edition_identifier", sa.String(length=256), nullable=True),
        sa.Column("work_identifier", sa.String(length=256), nullable=True),
        sa.Column(
            "mapping_metadata",
            JSON_VALUE,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        _created_at(),
        sa.CheckConstraint(
            "mapping_status IN "
            "('exact_edition', 'exact_work', 'ambiguous', 'unmatched')",
            name="ck_v2_book_mapping_status",
        ),
        sa.ForeignKeyConstraint(
            ["corpus_version_id"],
            ["v2_knowledge_corpus_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "corpus_version_id", "product_id", name="uq_v2_book_mapping_product"
        ),
    )
    op.create_index(
        "ix_v2_book_mappings_corpus_version_id",
        "v2_book_mappings",
        ["corpus_version_id"],
    )
    op.create_index(
        "ix_v2_book_mappings_product_id", "v2_book_mappings", ["product_id"]
    )


def _create_conversation_tables() -> None:
    op.create_table(
        "v2_conversations",
        _id(),
        *_owner_columns(),
        sa.Column("title", sa.String(length=160), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('shopper', 'merchant')", name="ck_v2_conversation_mode"
        ),
        sa.CheckConstraint("store_id = 'demo'", name="ck_v2_conversation_demo_store"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", *OWNER_NAMES, name="uq_v2_conversation_owner_identity"
        ),
    )
    op.create_index(
        "ix_v2_conversations_owner_updated",
        "v2_conversations",
        ["tenant_id", "principal_id", "mode", "store_id", "updated_at"],
    )
    op.create_table(
        "v2_turns",
        _id(),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        *_owner_columns(),
        sa.Column("client_turn_id", sa.String(length=128), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("request_payload", JSON_VALUE, nullable=False),
        sa.Column(
            "execution_state",
            sa.String(length=24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("dialogue_outcome", sa.String(length=32), nullable=True),
        sa.Column("result", JSON_VALUE, nullable=True),
        sa.Column("safe_error", JSON_VALUE, nullable=True),
        sa.Column("corpus_version_id", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_count", sa.Integer(), server_default="0", nullable=False),
        _created_at(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "execution_state IN ('pending', 'running', 'completed', 'failed', "
            "'cancelled', 'interrupted')",
            name="ck_v2_turn_execution_state",
        ),
        sa.CheckConstraint(
            "dialogue_outcome IS NULL OR dialogue_outcome IN "
            "('answered', 'needs_clarification', 'awaiting_confirmation', 'abstained')",
            name="ck_v2_turn_dialogue_outcome",
        ),
        sa.CheckConstraint(
            "(execution_state = 'completed' AND dialogue_outcome IS NOT NULL "
            "AND result IS NOT NULL AND safe_error IS NULL) OR "
            "(execution_state IN ('failed', 'interrupted') "
            "AND dialogue_outcome IS NULL "
            "AND result IS NULL AND safe_error IS NOT NULL) OR "
            "(execution_state IN ('pending', 'running', 'cancelled') "
            "AND dialogue_outcome IS NULL AND result IS NULL)",
            name="ck_v2_turn_result_shape",
        ),
        sa.CheckConstraint(
            "execution_state IN ('failed', 'interrupted') OR safe_error IS NULL",
            name="ck_v2_turn_nonterminal_error",
        ),
        sa.CheckConstraint(
            "(execution_state IN ('pending', 'running') AND completed_at IS NULL) OR "
            "(execution_state IN ('completed', 'failed', 'cancelled', 'interrupted') "
            "AND completed_at IS NOT NULL)",
            name="ck_v2_turn_completion_timestamp",
        ),
        sa.CheckConstraint(
            "recovery_count >= 0", name="ck_v2_turn_recovery_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", *OWNER_NAMES],
            ["v2_conversations.id", *[f"v2_conversations.{c}" for c in OWNER_NAMES]],
            name="fk_v2_turn_conversation_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["corpus_version_id"],
            ["v2_knowledge_corpus_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "client_turn_id", name="uq_v2_turn_client_retry"
        ),
        sa.UniqueConstraint("id", "conversation_id", name="uq_v2_turn_id_conversation"),
    )
    op.create_index(
        "ix_v2_turns_conversation_created",
        "v2_turns",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "ix_v2_turns_lease", "v2_turns", ["execution_state", "lease_expires_at"]
    )
    op.create_table(
        "v2_step_results",
        _id(),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("operation_key", sa.String(length=256), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("result", JSON_VALUE, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("data_version", sa.String(length=160), nullable=True),
        _created_at(),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("plan_revision >= 0", name="ck_v2_step_plan_revision"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'partial_success', 'failed')",
            name="ck_v2_step_status",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "conversation_id"],
            ["v2_turns.id", "v2_turns.conversation_id"],
            name="fk_v2_step_turn_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "operation_key", name="uq_v2_step_operation"),
    )
    op.create_table(
        "v2_preferences",
        _id(),
        *_owner_columns(),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("source_turn_id", sa.String(length=64), nullable=False),
        sa.Column("preference_key", sa.String(length=40), nullable=False),
        sa.Column("preference_value", JSON_VALUE, nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "preference_key IN ('genre', 'author', 'language', 'max_budget_vnd')",
            name="ck_v2_preference_key",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", *OWNER_NAMES],
            ["v2_conversations.id", *[f"v2_conversations.{c}" for c in OWNER_NAMES]],
            name="fk_v2_preference_conversation_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_turn_id", "conversation_id"],
            ["v2_turns.id", "v2_turns.conversation_id"],
            name="fk_v2_preference_source_turn",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            "preference_key",
            name="uq_v2_preference_owner_key",
        ),
    )


def _create_sandbox_tables() -> None:
    op.create_table(
        "v2_offers",
        _id(),
        sa.Column("tenant_id", sa.String(length=160), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("demo_price_vnd", sa.BigInteger(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("store_id = 'demo'", name="ck_v2_offer_demo_store"),
        sa.CheckConstraint(
            "demo_price_vnd > 0 AND demo_price_vnd <= 10000000000",
            name="ck_v2_offer_price_range",
        ),
        sa.CheckConstraint(
            "stock >= 0 AND stock <= 1000000", name="ck_v2_offer_stock_range"
        ),
        sa.CheckConstraint("version >= 1", name="ck_v2_offer_version_positive"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "store_id", "product_id", name="uq_v2_offer_product"
        ),
        sa.UniqueConstraint("id", "tenant_id", "store_id", name="uq_v2_offer_owner"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "store_id",
            "product_id",
            name="uq_v2_offer_owner_product",
        ),
    )
    op.create_index("ix_v2_offers_product_id", "v2_offers", ["product_id"])
    op.create_table(
        "v2_carts",
        _id(),
        sa.Column("tenant_id", sa.String(length=160), nullable=False),
        sa.Column("principal_id", sa.String(length=160), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="active", nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("store_id = 'demo'", name="ck_v2_cart_demo_store"),
        sa.CheckConstraint(
            "status IN ('active', 'checked_out', 'abandoned')",
            name="ck_v2_cart_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_v2_cart_version_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "tenant_id", "principal_id", "store_id", name="uq_v2_cart_owner"
        ),
    )
    op.create_index(
        "ix_v2_carts_owner_status",
        "v2_carts",
        ["tenant_id", "principal_id", "status"],
    )
    op.create_table(
        "v2_cart_lines",
        _id(),
        sa.Column("cart_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=160), nullable=False),
        sa.Column("principal_id", sa.String(length=160), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("offer_id", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("offer_version", sa.Integer(), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quantity > 0 AND quantity <= 1000000",
            name="ck_v2_cart_line_quantity_range",
        ),
        sa.CheckConstraint("offer_version >= 1", name="ck_v2_cart_line_offer_version"),
        sa.ForeignKeyConstraint(
            ["cart_id", "tenant_id", "principal_id", "store_id"],
            [
                "v2_carts.id",
                "v2_carts.tenant_id",
                "v2_carts.principal_id",
                "v2_carts.store_id",
            ],
            name="fk_v2_cart_line_cart_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["offer_id", "tenant_id", "store_id"],
            ["v2_offers.id", "v2_offers.tenant_id", "v2_offers.store_id"],
            name="fk_v2_cart_line_offer_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cart_id", "offer_id", name="uq_v2_cart_line_offer"),
    )
    op.create_table(
        "v2_orders",
        _id(),
        sa.Column("tenant_id", sa.String(length=160), nullable=False),
        sa.Column("principal_id", sa.String(length=160), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("cart_id", sa.String(length=64), nullable=False),
        sa.Column("cart_version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="confirmed", nullable=False
        ),
        sa.Column("total_vnd", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.CheckConstraint("status = 'confirmed'", name="ck_v2_order_status"),
        sa.CheckConstraint("cart_version >= 1", name="ck_v2_order_cart_version"),
        sa.CheckConstraint(
            "total_vnd >= 0 AND total_vnd <= 10000000000000000",
            name="ck_v2_order_total_range",
        ),
        sa.ForeignKeyConstraint(
            ["cart_id", "tenant_id", "principal_id", "store_id"],
            [
                "v2_carts.id",
                "v2_carts.tenant_id",
                "v2_carts.principal_id",
                "v2_carts.store_id",
            ],
            name="fk_v2_order_cart_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cart_id", name="uq_v2_order_cart"),
        sa.UniqueConstraint(
            "id", "tenant_id", "principal_id", "store_id", name="uq_v2_order_owner"
        ),
    )
    op.create_table(
        "v2_order_items",
        _id(),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=160), nullable=False),
        sa.Column("principal_id", sa.String(length=160), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("offer_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_vnd", sa.BigInteger(), nullable=False),
        sa.Column("product_snapshot", JSON_VALUE, nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "quantity > 0 AND quantity <= 1000000",
            name="ck_v2_order_item_quantity_range",
        ),
        sa.CheckConstraint(
            "unit_price_vnd > 0 AND unit_price_vnd <= 10000000000",
            name="ck_v2_order_item_price_range",
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "tenant_id", "principal_id", "store_id"],
            [
                "v2_orders.id",
                "v2_orders.tenant_id",
                "v2_orders.principal_id",
                "v2_orders.store_id",
            ],
            name="fk_v2_order_item_order_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["offer_id", "tenant_id", "store_id", "product_id"],
            [
                "v2_offers.id",
                "v2_offers.tenant_id",
                "v2_offers.store_id",
                "v2_offers.product_id",
            ],
            name="fk_v2_order_item_offer_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "offer_id", name="uq_v2_order_item_offer"),
    )
    op.create_table(
        "v2_proposals",
        _id(),
        *_owner_columns(),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("before_payload", JSON_VALUE, nullable=False),
        sa.Column("after_payload", JSON_VALUE, nullable=False),
        sa.Column("proposal_version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="proposed", nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", JSON_VALUE, nullable=True),
        _created_at(),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("proposal_version >= 1", name="ck_v2_proposal_version"),
        sa.CheckConstraint("target_version >= 1", name="ck_v2_proposal_target_version"),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected', 'executed', "
            "'expired', 'conflicted', 'failed')",
            name="ck_v2_proposal_status",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", *OWNER_NAMES],
            ["v2_conversations.id", *[f"v2_conversations.{c}" for c in OWNER_NAMES]],
            name="fk_v2_proposal_conversation_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "conversation_id"],
            ["v2_turns.id", "v2_turns.conversation_id"],
            name="fk_v2_proposal_turn_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            name="uq_v2_proposal_owner",
        ),
    )
    op.create_index(
        "ix_v2_proposals_owner_status",
        "v2_proposals",
        ["tenant_id", "principal_id", "status"],
    )
    op.create_table(
        "v2_action_idempotency",
        _id(),
        *_owner_columns(),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result", JSON_VALUE, nullable=True),
        _created_at(),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'committed', 'failed')",
            name="ck_v2_action_idempotency_status",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "tenant_id", "principal_id", "mode", "store_id"],
            [
                "v2_proposals.id",
                "v2_proposals.tenant_id",
                "v2_proposals.principal_id",
                "v2_proposals.mode",
                "v2_proposals.store_id",
            ],
            name="fk_v2_action_idempotency_proposal_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            "idempotency_key",
            name="uq_v2_action_idempotency_scope",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            name="uq_v2_action_idempotency_owner",
        ),
    )
    op.create_table(
        "v2_action_audit",
        _id(),
        *_owner_columns(),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("action_idempotency_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column(
            "payload", JSON_VALUE, server_default=sa.text("'{}'"), nullable=False
        ),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["proposal_id", "tenant_id", "principal_id", "mode", "store_id"],
            [
                "v2_proposals.id",
                "v2_proposals.tenant_id",
                "v2_proposals.principal_id",
                "v2_proposals.mode",
                "v2_proposals.store_id",
            ],
            name="fk_v2_action_audit_proposal_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "action_idempotency_id",
                "tenant_id",
                "principal_id",
                "mode",
                "store_id",
            ],
            [
                "v2_action_idempotency.id",
                "v2_action_idempotency.tenant_id",
                "v2_action_idempotency.principal_id",
                "v2_action_idempotency.mode",
                "v2_action_idempotency.store_id",
            ],
            name="fk_v2_action_audit_idempotency_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_v2_action_audit_proposal_created",
        "v2_action_audit",
        ["proposal_id", "created_at"],
    )


def _create_postgresql_immutability_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION v2_reject_conversation_identity_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.tenant_id, NEW.principal_id, NEW.mode, NEW.store_id)
                IS DISTINCT FROM
               (OLD.tenant_id, OLD.principal_id, OLD.mode, OLD.store_id) THEN
                RAISE EXCEPTION 'v2 conversation identity is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_v2_conversation_identity_immutable
        BEFORE UPDATE ON v2_conversations
        FOR EACH ROW EXECUTE FUNCTION v2_reject_conversation_identity_update()
        """
    )
    op.execute(
        """
        CREATE FUNCTION v2_reject_immutable_order_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'v2 orders and order items are immutable'
                USING ERRCODE = '23514';
        END;
        $$
        """
    )
    for table_name in ("v2_orders", "v2_order_items"):
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_immutable "
            f"BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_order_change()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER trg_v2_order_items_immutable ON v2_order_items")
        op.execute("DROP TRIGGER trg_v2_orders_immutable ON v2_orders")
        op.execute("DROP FUNCTION v2_reject_immutable_order_change()")
        op.execute(
            "DROP TRIGGER trg_v2_conversation_identity_immutable ON v2_conversations"
        )
        op.execute("DROP FUNCTION v2_reject_conversation_identity_update()")

    for table_name in (
        "v2_action_audit",
        "v2_action_idempotency",
        "v2_proposals",
        "v2_order_items",
        "v2_orders",
        "v2_cart_lines",
        "v2_carts",
        "v2_offers",
        "v2_preferences",
        "v2_step_results",
        "v2_turns",
        "v2_conversations",
        "v2_book_mappings",
        "v2_knowledge_vectors",
        "v2_knowledge_chunks",
        "v2_knowledge_documents",
        "v2_knowledge_index_manifests",
        "v2_knowledge_corpus_versions",
    ):
        op.drop_table(table_name)
