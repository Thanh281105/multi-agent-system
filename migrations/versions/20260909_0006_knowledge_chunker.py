"""Version knowledge chunks by their immutable chunker layout.

Revision ID: 20260909_0006
Revises: 20260909_0005
Create Date: 2026-09-09
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "20260909_0006"
down_revision: str | None = "20260909_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_CHUNKER_VERSION = "legacy_pre_p3"
_CHUNKER_VERSION_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,119}$")
_OLD_UNIQUE = "uq_v2_chunk_document_index"
_NEW_UNIQUE = "uq_v2_chunk_document_chunker_index"


def upgrade() -> None:
    assignments = _validated_chunker_assignments(op.get_bind())
    op.add_column(
        "v2_knowledge_chunks",
        sa.Column("chunker_version", sa.String(length=120), nullable=True),
    )
    _apply_chunker_assignments(op.get_bind(), assignments)

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("v2_knowledge_chunks", recreate="always") as batch:
            batch.alter_column(
                "chunker_version",
                existing_type=sa.String(length=120),
                nullable=False,
            )
            batch.drop_constraint(_OLD_UNIQUE, type_="unique")
            batch.create_unique_constraint(
                _NEW_UNIQUE,
                ["document_id", "chunker_version", "chunk_index"],
            )
        return

    op.alter_column(
        "v2_knowledge_chunks",
        "chunker_version",
        existing_type=sa.String(length=120),
        nullable=False,
    )
    op.drop_constraint(_OLD_UNIQUE, "v2_knowledge_chunks", type_="unique")
    op.create_unique_constraint(
        _NEW_UNIQUE,
        "v2_knowledge_chunks",
        ["document_id", "chunker_version", "chunk_index"],
    )


def downgrade() -> None:
    collision = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT document_id, chunk_index, COUNT(*) AS chunk_count "
                "FROM v2_knowledge_chunks "
                "GROUP BY document_id, chunk_index "
                "HAVING COUNT(*) > 1 "
                "ORDER BY document_id, chunk_index "
                "LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    if collision is not None:
        raise RuntimeError(
            "cannot downgrade knowledge chunks to 20260909_0005: "
            "multiple chunker versions use document "
            f"{collision['document_id']!r} chunk index "
            f"{collision['chunk_index']!r}"
        )
    _validate_downgrade_reconstruction(op.get_bind())

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("v2_knowledge_chunks", recreate="always") as batch:
            batch.drop_constraint(_NEW_UNIQUE, type_="unique")
            batch.create_unique_constraint(
                _OLD_UNIQUE,
                ["document_id", "chunk_index"],
            )
            batch.drop_column("chunker_version")
        return

    op.drop_constraint(_NEW_UNIQUE, "v2_knowledge_chunks", type_="unique")
    op.create_unique_constraint(
        _OLD_UNIQUE,
        "v2_knowledge_chunks",
        ["document_id", "chunk_index"],
    )
    op.drop_column("v2_knowledge_chunks", "chunker_version")


def _validated_chunker_assignments(connection: Connection) -> list[dict[str, str]]:
    chunks = (
        connection.execute(
            sa.text(
                "SELECT id AS chunk_id, metadata AS chunk_metadata "
                "FROM v2_knowledge_chunks ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    index_rows = connection.execute(
        sa.text(
            "SELECT vectors.chunk_id, manifests.chunker_version "
            "FROM v2_knowledge_vectors AS vectors "
            "JOIN v2_knowledge_index_manifests AS manifests "
            "ON manifests.id = vectors.index_manifest_id "
            "AND manifests.corpus_version_id = vectors.corpus_version_id "
            "ORDER BY vectors.chunk_id, manifests.chunker_version"
        )
    ).mappings()

    index_versions: dict[str, set[str]] = {}
    for row in index_rows:
        chunk_id = str(row["chunk_id"])
        version = _validated_version(
            row["chunker_version"],
            chunk_id=chunk_id,
            evidence="associated index manifest",
        )
        index_versions.setdefault(chunk_id, set()).add(version)

    updates: list[dict[str, str]] = []
    for row in chunks:
        chunk_id = str(row["chunk_id"])
        metadata_version = _metadata_version(row["chunk_metadata"], chunk_id)
        associated_versions = index_versions.get(chunk_id, set())
        if len(associated_versions) > 1:
            raise RuntimeError(
                "cannot migrate knowledge chunk "
                f"{chunk_id!r}: multiple associated index chunker versions"
            )
        index_version = next(iter(associated_versions), None)
        if (
            metadata_version is not None
            and index_version is not None
            and metadata_version != index_version
        ):
            raise RuntimeError(
                "cannot migrate knowledge chunk "
                f"{chunk_id!r}: conflicting metadata and index chunker versions"
            )
        updates.append(
            {
                "chunk_id": chunk_id,
                "chunker_version": (
                    metadata_version or index_version or LEGACY_CHUNKER_VERSION
                ),
            }
        )

    return updates


def _apply_chunker_assignments(
    connection: Connection,
    assignments: list[dict[str, str]],
) -> None:
    if not assignments:
        return
    connection.execute(
        sa.text(
            "UPDATE v2_knowledge_chunks "
            "SET chunker_version = :chunker_version WHERE id = :chunk_id"
        ),
        assignments,
    )


def _validate_downgrade_reconstruction(connection: Connection) -> None:
    try:
        reconstructable = {
            assignment["chunk_id"]: assignment["chunker_version"]
            for assignment in _validated_chunker_assignments(connection)
        }
    except RuntimeError as exc:
        raise RuntimeError(
            "cannot downgrade knowledge chunks to 20260909_0005: "
            f"existing evidence is not reconstructable ({exc})"
        ) from exc

    current_rows = connection.execute(
        sa.text(
            "SELECT id AS chunk_id, chunker_version "
            "FROM v2_knowledge_chunks ORDER BY id"
        )
    ).mappings()
    for row in current_rows:
        chunk_id = str(row["chunk_id"])
        current_version = str(row["chunker_version"])
        if reconstructable[chunk_id] != current_version:
            raise RuntimeError(
                "cannot downgrade knowledge chunks to 20260909_0005: "
                f"chunk {chunk_id!r} would change chunker version from "
                f"{current_version!r} to {reconstructable[chunk_id]!r} on reapply"
            )


def _metadata_version(raw_metadata: object, chunk_id: str) -> str | None:
    metadata: object = raw_metadata
    if isinstance(metadata, (bytes, bytearray)):
        metadata = metadata.decode("utf-8")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"cannot migrate knowledge chunk {chunk_id!r}: invalid metadata JSON"
            ) from exc
    if not isinstance(metadata, Mapping):
        raise RuntimeError(
            f"cannot migrate knowledge chunk {chunk_id!r}: metadata is not an object"
        )
    if "chunker_version" not in metadata:
        return None
    return _validated_version(
        metadata["chunker_version"],
        chunk_id=chunk_id,
        evidence="metadata",
    )


def _validated_version(value: object, *, chunk_id: str, evidence: str) -> str:
    if not isinstance(value, str) or _CHUNKER_VERSION_PATTERN.fullmatch(value) is None:
        raise RuntimeError(
            "cannot migrate knowledge chunk "
            f"{chunk_id!r}: invalid {evidence} chunker version"
        )
    return value
