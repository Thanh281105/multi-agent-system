"""Durable ORM schema for the isolated v2 conversation platform."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now

JSON_VALUE = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True), "postgresql"
)
OWNER_COLUMNS = ("tenant_id", "principal_id", "mode", "store_id")


def _json_default(value: str) -> Any:
    return text(f"'{value}'")


class V2KnowledgeCorpusVersion(Base):
    __tablename__ = "v2_knowledge_corpus_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_v2_corpus_versions_status",
        ),
        UniqueConstraint("corpus_name", "version", name="uq_v2_corpus_name_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=_json_default("{}")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class V2KnowledgeIndexManifest(Base):
    __tablename__ = "v2_knowledge_index_manifests"
    __table_args__ = (
        CheckConstraint(
            "embedding_dimension > 0", name="ck_v2_index_dimension_positive"
        ),
        UniqueConstraint(
            "corpus_version_id", "fingerprint", name="uq_v2_index_corpus_fingerprint"
        ),
        UniqueConstraint("id", "corpus_version_id", name="uq_v2_index_id_corpus"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("v2_knowledge_corpus_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(160), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(120), nullable=False)
    enrichment_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=_json_default("{}")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class V2KnowledgeDocument(Base):
    __tablename__ = "v2_knowledge_documents"
    __table_args__ = (
        UniqueConstraint(
            "corpus_version_id",
            "source_url",
            "document_version",
            name="uq_v2_document_source_version",
        ),
        UniqueConstraint("id", "corpus_version_id", name="uq_v2_document_id_corpus"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("v2_knowledge_corpus_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    use_basis: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_version: Mapped[str] = mapped_column(String(120), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    access_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=_json_default("{}")
    )
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=_json_default("{}")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class V2KnowledgeChunk(Base):
    __tablename__ = "v2_knowledge_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "corpus_version_id"],
            ["v2_knowledge_documents.id", "v2_knowledge_documents.corpus_version_id"],
            ondelete="CASCADE",
            name="fk_v2_chunk_document_corpus",
        ),
        CheckConstraint("chunk_index >= 0", name="ck_v2_chunk_index_non_negative"),
        CheckConstraint("token_count >= 0", name="ck_v2_chunk_tokens_non_negative"),
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_v2_chunk_document_index"
        ),
        UniqueConstraint("id", "corpus_version_id", name="uq_v2_chunk_id_corpus"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_version_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON_VALUE,
        nullable=False,
        default=dict,
        server_default=_json_default("{}"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class V2KnowledgeVector(Base):
    """One embedding per chunk and immutable index fingerprint."""

    __tablename__ = "v2_knowledge_vectors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_id", "corpus_version_id"],
            ["v2_knowledge_chunks.id", "v2_knowledge_chunks.corpus_version_id"],
            ondelete="CASCADE",
            name="fk_v2_vector_chunk_corpus",
        ),
        ForeignKeyConstraint(
            ["index_manifest_id", "corpus_version_id"],
            [
                "v2_knowledge_index_manifests.id",
                "v2_knowledge_index_manifests.corpus_version_id",
            ],
            ondelete="CASCADE",
            name="fk_v2_vector_index_corpus",
        ),
        UniqueConstraint(
            "chunk_id", "index_manifest_id", name="uq_v2_vector_chunk_index"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)
    index_manifest_id: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_version_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    vector: Mapped[list[float]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class V2BookMapping(Base):
    __tablename__ = "v2_book_mappings"
    __table_args__ = (
        CheckConstraint(
            "mapping_status IN "
            "('exact_edition', 'exact_work', 'ambiguous', 'unmatched')",
            name="ck_v2_book_mapping_status",
        ),
        UniqueConstraint(
            "corpus_version_id", "product_id", name="uq_v2_book_mapping_product"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("v2_knowledge_corpus_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mapping_status: Mapped[str] = mapped_column(String(24), nullable=False)
    edition_identifier: Mapped[str | None] = mapped_column(String(256), nullable=True)
    work_identifier: Mapped[str | None] = mapped_column(String(256), nullable=True)
    mapping_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=_json_default("{}")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class V2Conversation(Base):
    __tablename__ = "v2_conversations"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('shopper', 'merchant')", name="ck_v2_conversation_mode"
        ),
        CheckConstraint("store_id = 'demo'", name="ck_v2_conversation_demo_store"),
        UniqueConstraint(
            "id", *OWNER_COLUMNS, name="uq_v2_conversation_owner_identity"
        ),
        Index(
            "ix_v2_conversations_owner_updated",
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, default="demo")
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
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
        server_default=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class V2Turn(Base):
    __tablename__ = "v2_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", *OWNER_COLUMNS],
            ["v2_conversations.id", *[f"v2_conversations.{c}" for c in OWNER_COLUMNS]],
            ondelete="CASCADE",
            name="fk_v2_turn_conversation_owner",
        ),
        CheckConstraint(
            "execution_state IN ('pending', 'running', 'completed', 'failed', "
            "'cancelled', 'interrupted')",
            name="ck_v2_turn_execution_state",
        ),
        CheckConstraint(
            "dialogue_outcome IS NULL OR dialogue_outcome IN "
            "('answered', 'needs_clarification', 'awaiting_confirmation', 'abstained')",
            name="ck_v2_turn_dialogue_outcome",
        ),
        CheckConstraint(
            "(execution_state = 'completed' AND dialogue_outcome IS NOT NULL "
            "AND result IS NOT NULL AND safe_error IS NULL) OR "
            "(execution_state IN ('failed', 'interrupted') "
            "AND dialogue_outcome IS NULL "
            "AND result IS NULL AND safe_error IS NOT NULL) OR "
            "(execution_state IN ('pending', 'running', 'cancelled') "
            "AND dialogue_outcome IS NULL AND result IS NULL)",
            name="ck_v2_turn_result_shape",
        ),
        CheckConstraint(
            "execution_state IN ('failed', 'interrupted') OR safe_error IS NULL",
            name="ck_v2_turn_nonterminal_error",
        ),
        CheckConstraint(
            "(execution_state IN ('pending', 'running') AND completed_at IS NULL) OR "
            "(execution_state IN ('completed', 'failed', 'cancelled', 'interrupted') "
            "AND completed_at IS NOT NULL)",
            name="ck_v2_turn_completion_timestamp",
        ),
        CheckConstraint("recovery_count >= 0", name="ck_v2_turn_recovery_non_negative"),
        UniqueConstraint(
            "conversation_id", "client_turn_id", name="uq_v2_turn_client_retry"
        ),
        UniqueConstraint("id", "conversation_id", name="uq_v2_turn_id_conversation"),
        Index("ix_v2_turns_conversation_created", "conversation_id", "created_at"),
        Index("ix_v2_turns_lease", "execution_state", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    client_turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    execution_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    dialogue_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    safe_error: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    corpus_version_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("v2_knowledge_corpus_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovery_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class V2StepResult(Base):
    __tablename__ = "v2_step_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["turn_id", "conversation_id"],
            ["v2_turns.id", "v2_turns.conversation_id"],
            ondelete="CASCADE",
            name="fk_v2_step_turn_conversation",
        ),
        CheckConstraint("plan_revision >= 0", name="ck_v2_step_plan_revision"),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'partial_success', 'failed')",
            name="ck_v2_step_status",
        ),
        UniqueConstraint("turn_id", "operation_key", name="uq_v2_step_operation"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=_json_default("{}")
    )
    data_version: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class V2Preference(Base):
    __tablename__ = "v2_preferences"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", *OWNER_COLUMNS],
            ["v2_conversations.id", *[f"v2_conversations.{c}" for c in OWNER_COLUMNS]],
            ondelete="CASCADE",
            name="fk_v2_preference_conversation_owner",
        ),
        ForeignKeyConstraint(
            ["source_turn_id", "conversation_id"],
            ["v2_turns.id", "v2_turns.conversation_id"],
            ondelete="CASCADE",
            name="fk_v2_preference_source_turn",
        ),
        CheckConstraint(
            "preference_key IN ('genre', 'author', 'language', 'max_budget_vnd')",
            name="ck_v2_preference_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            "preference_key",
            name="uq_v2_preference_owner_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    preference_key: Mapped[str] = mapped_column(String(40), nullable=False)
    preference_value: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
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
        server_default=func.now(),
    )


class V2Offer(Base):
    __tablename__ = "v2_offers"
    __table_args__ = (
        CheckConstraint("store_id = 'demo'", name="ck_v2_offer_demo_store"),
        CheckConstraint(
            "demo_price_vnd > 0 AND demo_price_vnd <= 10000000000",
            name="ck_v2_offer_price_range",
        ),
        CheckConstraint(
            "stock >= 0 AND stock <= 1000000", name="ck_v2_offer_stock_range"
        ),
        CheckConstraint("version >= 1", name="ck_v2_offer_version_positive"),
        UniqueConstraint(
            "tenant_id", "store_id", "product_id", name="uq_v2_offer_product"
        ),
        UniqueConstraint("id", "tenant_id", "store_id", name="uq_v2_offer_owner"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "store_id",
            "product_id",
            name="uq_v2_offer_owner_product",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, default="demo")
    product_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    demo_price_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
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
        server_default=func.now(),
    )


class V2Cart(Base):
    __tablename__ = "v2_carts"
    __table_args__ = (
        CheckConstraint("store_id = 'demo'", name="ck_v2_cart_demo_store"),
        CheckConstraint(
            "status IN ('active', 'checked_out', 'abandoned')", name="ck_v2_cart_status"
        ),
        CheckConstraint("version >= 1", name="ck_v2_cart_version_positive"),
        UniqueConstraint(
            "id", "tenant_id", "principal_id", "store_id", name="uq_v2_cart_owner"
        ),
        Index("ix_v2_carts_owner_status", "tenant_id", "principal_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False, default="demo")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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
        server_default=func.now(),
    )


class V2CartLine(Base):
    __tablename__ = "v2_cart_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cart_id", "tenant_id", "principal_id", "store_id"],
            [
                "v2_carts.id",
                "v2_carts.tenant_id",
                "v2_carts.principal_id",
                "v2_carts.store_id",
            ],
            ondelete="CASCADE",
            name="fk_v2_cart_line_cart_owner",
        ),
        ForeignKeyConstraint(
            ["offer_id", "tenant_id", "store_id"],
            ["v2_offers.id", "v2_offers.tenant_id", "v2_offers.store_id"],
            ondelete="RESTRICT",
            name="fk_v2_cart_line_offer_owner",
        ),
        CheckConstraint(
            "quantity > 0 AND quantity <= 1000000",
            name="ck_v2_cart_line_quantity_range",
        ),
        CheckConstraint("offer_version >= 1", name="ck_v2_cart_line_offer_version"),
        UniqueConstraint("cart_id", "offer_id", name="uq_v2_cart_line_offer"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cart_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    offer_version: Mapped[int] = mapped_column(Integer, nullable=False)
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
        server_default=func.now(),
    )


class V2Order(Base):
    __tablename__ = "v2_orders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cart_id", "tenant_id", "principal_id", "store_id"],
            [
                "v2_carts.id",
                "v2_carts.tenant_id",
                "v2_carts.principal_id",
                "v2_carts.store_id",
            ],
            ondelete="RESTRICT",
            name="fk_v2_order_cart_owner",
        ),
        CheckConstraint("status = 'confirmed'", name="ck_v2_order_status"),
        CheckConstraint("cart_version >= 1", name="ck_v2_order_cart_version"),
        CheckConstraint(
            "total_vnd >= 0 AND total_vnd <= 10000000000000000",
            name="ck_v2_order_total_range",
        ),
        UniqueConstraint("cart_id", name="uq_v2_order_cart"),
        UniqueConstraint(
            "id", "tenant_id", "principal_id", "store_id", name="uq_v2_order_owner"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cart_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cart_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="confirmed", server_default="confirmed"
    )
    total_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class V2OrderItem(Base):
    __tablename__ = "v2_order_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id", "tenant_id", "principal_id", "store_id"],
            [
                "v2_orders.id",
                "v2_orders.tenant_id",
                "v2_orders.principal_id",
                "v2_orders.store_id",
            ],
            ondelete="RESTRICT",
            name="fk_v2_order_item_order_owner",
        ),
        ForeignKeyConstraint(
            ["offer_id", "tenant_id", "store_id", "product_id"],
            [
                "v2_offers.id",
                "v2_offers.tenant_id",
                "v2_offers.store_id",
                "v2_offers.product_id",
            ],
            ondelete="RESTRICT",
            name="fk_v2_order_item_offer_owner",
        ),
        CheckConstraint(
            "quantity > 0 AND quantity <= 1000000",
            name="ck_v2_order_item_quantity_range",
        ),
        CheckConstraint(
            "unit_price_vnd > 0 AND unit_price_vnd <= 10000000000",
            name="ck_v2_order_item_price_range",
        ),
        UniqueConstraint("order_id", "offer_id", name="uq_v2_order_item_offer"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_vnd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class V2Proposal(Base):
    __tablename__ = "v2_proposals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", *OWNER_COLUMNS],
            ["v2_conversations.id", *[f"v2_conversations.{c}" for c in OWNER_COLUMNS]],
            ondelete="CASCADE",
            name="fk_v2_proposal_conversation_owner",
        ),
        ForeignKeyConstraint(
            ["turn_id", "conversation_id"],
            ["v2_turns.id", "v2_turns.conversation_id"],
            ondelete="CASCADE",
            name="fk_v2_proposal_turn_conversation",
        ),
        CheckConstraint("proposal_version >= 1", name="ck_v2_proposal_version"),
        CheckConstraint("target_version >= 1", name="ck_v2_proposal_target_version"),
        CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected', 'executed', "
            "'expired', 'conflicted', 'failed')",
            name="ck_v2_proposal_status",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            name="uq_v2_proposal_owner",
        ),
        Index("ix_v2_proposals_owner_status", "tenant_id", "principal_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    after_payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed", server_default="proposed"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class V2ActionIdempotency(Base):
    __tablename__ = "v2_action_idempotency"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "tenant_id", "principal_id", "mode", "store_id"],
            [
                "v2_proposals.id",
                "v2_proposals.tenant_id",
                "v2_proposals.principal_id",
                "v2_proposals.mode",
                "v2_proposals.store_id",
            ],
            ondelete="RESTRICT",
            name="fk_v2_action_idempotency_proposal_owner",
        ),
        CheckConstraint(
            "status IN ('started', 'committed', 'failed')",
            name="ck_v2_action_idempotency_status",
        ),
        UniqueConstraint(
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            "idempotency_key",
            name="uq_v2_action_idempotency_scope",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "principal_id",
            "mode",
            "store_id",
            name="uq_v2_action_idempotency_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class V2ActionAudit(Base):
    __tablename__ = "v2_action_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "tenant_id", "principal_id", "mode", "store_id"],
            [
                "v2_proposals.id",
                "v2_proposals.tenant_id",
                "v2_proposals.principal_id",
                "v2_proposals.mode",
                "v2_proposals.store_id",
            ],
            ondelete="RESTRICT",
            name="fk_v2_action_audit_proposal_owner",
        ),
        ForeignKeyConstraint(
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
            ondelete="RESTRICT",
            name="fk_v2_action_audit_idempotency_owner",
        ),
        Index("ix_v2_action_audit_proposal_created", "proposal_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_idempotency_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict, server_default=_json_default("{}")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
