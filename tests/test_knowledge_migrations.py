"""Migration coverage for immutable knowledge chunker layouts."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.migrate import (
    EXPECTED_DATABASE_REVISION,
    _migration_config,
    check_database_schema,
    upgrade_database,
)
from tests.v2_postgres_support import disposable_postgres_database

_PREVIOUS_REVISION = "20260909_0005"
_LEGACY_CHUNKER_VERSION = "legacy_pre_p3"
_NEW_UNIQUE = "uq_v2_chunk_document_chunker_index"
_OLD_UNIQUE = "uq_v2_chunk_document_index"


def test_sqlite_chunker_migration_preserves_rows_and_roundtrips(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'chunks.db').as_posix()}"
    upgrade_database(database_url, revision=_PREVIOUS_REVISION)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            _seed_preserved_knowledge(connection)
            before = _knowledge_snapshot(connection)

        upgrade_database(database_url)
        _assert_chunker_schema(engine)
        with engine.connect() as connection:
            assert _knowledge_snapshot(connection) == before
            assert _chunker_assignments(connection) == _expected_assignments()

        _downgrade_database(engine, _PREVIOUS_REVISION)
        assert "chunker_version" not in _column_names(engine)
        _assert_unique_constraint(engine, _OLD_UNIQUE, ["document_id", "chunk_index"])
        with engine.connect() as connection:
            assert _knowledge_snapshot(connection) == before

        upgrade_database(database_url)
        check_database_schema(database_url)
        _assert_chunker_schema(engine)
        with engine.connect() as connection:
            assert _knowledge_snapshot(connection) == before
            assert _chunker_assignments(connection) == _expected_assignments()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("conflicting", "conflicting metadata and index chunker versions"),
        ("ambiguous", "multiple associated index chunker versions"),
    ],
)
def test_sqlite_upgrade_refuses_unsafe_evidence_before_schema_mutation(
    tmp_path: Path,
    scenario: str,
    message: str,
) -> None:
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / f'unsafe-{scenario}.db').as_posix()}"
    )
    upgrade_database(database_url, revision=_PREVIOUS_REVISION)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            _seed_unsafe_evidence(connection, scenario)
            before = _knowledge_snapshot(connection)

        for _ in range(2):
            with pytest.raises(RuntimeError, match=message):
                upgrade_database(database_url)
            assert "chunker_version" not in _column_names(engine)
            with engine.connect() as connection:
                assert _revision(connection) == _PREVIOUS_REVISION
                assert _knowledge_snapshot(connection) == before
    finally:
        engine.dispose()


def test_postgres_chunker_migration_preserves_rows_constraints_and_foreign_keys() -> (
    None
):
    with disposable_postgres_database("thanh_v2_p2_chunk_") as database_url:
        upgrade_database(database_url, revision=_PREVIOUS_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_preserved_knowledge(connection)
                before = _knowledge_snapshot(connection)

            upgrade_database(database_url)
            check_database_schema(database_url)
            _assert_chunker_schema(engine)
            with engine.connect() as connection:
                assert _knowledge_snapshot(connection) == before
                assert _chunker_assignments(connection) == _expected_assignments()
                assert connection.execute(
                    text(
                        "SELECT vectors.id, chunks.id, manifests.id, "
                        "chunks.corpus_version_id "
                        "FROM v2_knowledge_vectors AS vectors "
                        "JOIN v2_knowledge_chunks AS chunks "
                        "ON chunks.id = vectors.chunk_id "
                        "AND chunks.corpus_version_id = vectors.corpus_version_id "
                        "JOIN v2_knowledge_index_manifests AS manifests "
                        "ON manifests.id = vectors.index_manifest_id "
                        "AND manifests.corpus_version_id = vectors.corpus_version_id "
                        "ORDER BY vectors.id"
                    )
                ).all() == [
                    ("vector_both", "chunk_both", "index_metadata", "corpus_a"),
                    ("vector_index", "chunk_index", "index_derived", "corpus_a"),
                ]

            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO v2_knowledge_chunks "
                        "(id, document_id, corpus_version_id, chunker_version, "
                        "chunk_index, content, token_count, metadata) VALUES "
                        "('chunk_head_safe', 'document_a', 'corpus_a', 'head_v1', "
                        "4, 'Head row with reconstructable evidence', 4, "
                        '\'{"chunker_version": "head_v1"}\')'
                    )
                )
                safe_before = _knowledge_snapshot(connection)

            with engine.begin() as connection:
                _insert_chunk(
                    connection,
                    chunk_id="chunk_other_layout",
                    chunker_version="other_v1",
                    chunk_index=0,
                )
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    _insert_chunk(
                        connection,
                        chunk_id="chunk_duplicate_layout",
                        chunker_version="metadata_v1",
                        chunk_index=0,
                    )

            with engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM v2_knowledge_chunks "
                        "WHERE id = 'chunk_other_layout'"
                    )
                )
            _downgrade_database(engine, _PREVIOUS_REVISION)
            assert "chunker_version" not in _column_names(engine)
            _assert_unique_constraint(
                engine, _OLD_UNIQUE, ["document_id", "chunk_index"]
            )
            with engine.connect() as connection:
                assert _knowledge_snapshot(connection) == safe_before

            upgrade_database(database_url)
            check_database_schema(database_url)
            with engine.connect() as connection:
                assert _knowledge_snapshot(connection) == safe_before
                assert _chunker_assignments(connection) == [
                    ("chunk_both", "metadata_v1"),
                    ("chunk_head_safe", "head_v1"),
                    ("chunk_index", "index_v1"),
                    ("chunk_legacy", _LEGACY_CHUNKER_VERSION),
                    ("chunk_metadata", "metadata_v1"),
                ]
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("conflicting", "conflicting metadata and index chunker versions"),
        ("ambiguous", "multiple associated index chunker versions"),
    ],
)
def test_postgres_upgrade_refuses_unsafe_chunker_evidence_without_data_loss(
    scenario: str,
    message: str,
) -> None:
    with disposable_postgres_database("thanh_v2_p2_chunk_") as database_url:
        upgrade_database(database_url, revision=_PREVIOUS_REVISION)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_unsafe_evidence(connection, scenario)
                before = _knowledge_snapshot(connection)

            with pytest.raises(RuntimeError, match=message):
                upgrade_database(database_url)

            assert "chunker_version" not in _column_names(engine)
            with engine.connect() as connection:
                assert _revision(connection) == _PREVIOUS_REVISION
                assert _knowledge_snapshot(connection) == before
        finally:
            engine.dispose()


def test_postgres_downgrade_refuses_incompatible_layouts_without_data_loss() -> None:
    with disposable_postgres_database("thanh_v2_p2_chunk_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_head_document(connection)
                _insert_chunk(
                    connection,
                    chunk_id="chunk_layout_a",
                    chunker_version="layout_a",
                    chunk_index=0,
                )
                _insert_chunk(
                    connection,
                    chunk_id="chunk_layout_b",
                    chunker_version="layout_b",
                    chunk_index=0,
                )
                before = _knowledge_snapshot(connection)
                assignments = _chunker_assignments(connection)

            with pytest.raises(RuntimeError, match="multiple chunker versions"):
                _downgrade_database(engine, _PREVIOUS_REVISION)

            check_database_schema(database_url)
            _assert_chunker_schema(engine)
            with engine.connect() as connection:
                assert _revision(connection) == EXPECTED_DATABASE_REVISION
                assert _knowledge_snapshot(connection) == before
                assert _chunker_assignments(connection) == assignments
        finally:
            engine.dispose()


def test_postgres_downgrade_refuses_unreconstructable_new_chunker() -> None:
    with disposable_postgres_database("thanh_v2_p2_chunk_") as database_url:
        upgrade_database(database_url)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_head_document(connection)
                _insert_chunk(
                    connection,
                    chunk_id="chunk_new_only",
                    chunker_version="new_only_v1",
                    chunk_index=0,
                )
                before = _knowledge_snapshot(connection)
                assignments = _chunker_assignments(connection)

            with pytest.raises(RuntimeError, match="would change chunker version"):
                _downgrade_database(engine, _PREVIOUS_REVISION)

            check_database_schema(database_url)
            _assert_chunker_schema(engine)
            with engine.connect() as connection:
                assert _revision(connection) == EXPECTED_DATABASE_REVISION
                assert _knowledge_snapshot(connection) == before
                assert _chunker_assignments(connection) == assignments
        finally:
            engine.dispose()


def _seed_preserved_knowledge(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_corpus_versions "
            "(id, corpus_name, version, status) VALUES "
            "('corpus_a', 'synthetic', 'source_v1', 'draft')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_index_manifests "
            "(id, corpus_version_id, fingerprint, embedding_model, "
            "embedding_dimension, chunker_version, enrichment_policy_version) "
            "VALUES "
            "('index_derived', 'corpus_a', :derived_fingerprint, 'hashing', 2, "
            "'index_v1', 'none_v1'), "
            "('index_metadata', 'corpus_a', :metadata_fingerprint, 'hashing', 2, "
            "'metadata_v1', 'none_v1')"
        ),
        {
            "derived_fingerprint": "a" * 64,
            "metadata_fingerprint": "b" * 64,
        },
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_documents "
            "(id, corpus_version_id, source_url, title, use_basis, content_hash, "
            "document_version, retrieved_at) VALUES "
            "('document_a', 'corpus_a', 'https://example.test/source', "
            "'Synthetic source', 'public metadata', :content_hash, 'document_v1', "
            "'2026-09-09 00:00:00+00:00')"
        ),
        {"content_hash": "c" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_chunks "
            "(id, document_id, corpus_version_id, chunk_index, content, token_count, "
            "metadata) VALUES "
            "('chunk_metadata', 'document_a', 'corpus_a', 0, "
            "'Metadata evidence', 2, "
            '\'{"chunker_version": "metadata_v1", "source": "test"}\'), '
            "('chunk_index', 'document_a', 'corpus_a', 1, "
            "'Index evidence', 2, '{}'), "
            "('chunk_both', 'document_a', 'corpus_a', 2, "
            "'Matching evidence', 2, "
            '\'{"chunker_version": "metadata_v1"}\'), '
            "('chunk_legacy', 'document_a', 'corpus_a', 3, "
            "'No pre-P3 evidence', 3, '{}')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_vectors "
            "(id, chunk_id, index_manifest_id, corpus_version_id, vector) VALUES "
            "('vector_index', 'chunk_index', 'index_derived', 'corpus_a', "
            "'[0.1, 0.2]'), "
            "('vector_both', 'chunk_both', 'index_metadata', 'corpus_a', "
            "'[0.3, 0.4]')"
        )
    )


def _seed_unsafe_evidence(connection: Connection, scenario: str) -> None:
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_corpus_versions "
            "(id, corpus_name, version, status) VALUES "
            "('corpus_a', 'synthetic', 'source_v1', 'draft')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_documents "
            "(id, corpus_version_id, source_url, title, use_basis, content_hash, "
            "document_version, retrieved_at) VALUES "
            "('document_a', 'corpus_a', 'https://example.test/source', "
            "'Synthetic source', 'public metadata', :content_hash, 'document_v1', "
            "'2026-09-09 00:00:00+00:00')"
        ),
        {"content_hash": "c" * 64},
    )
    metadata = (
        '\'{"chunker_version": "metadata_v1"}\''
        if scenario == "conflicting"
        else "'{}'"
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_chunks "
            "(id, document_id, corpus_version_id, chunk_index, content, token_count, "
            "metadata) VALUES ('chunk_a', 'document_a', 'corpus_a', 0, "
            f"'Unsafe evidence', 2, {metadata})"
        )
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_index_manifests "
            "(id, corpus_version_id, fingerprint, embedding_model, "
            "embedding_dimension, chunker_version, enrichment_policy_version) "
            "VALUES ('index_a', 'corpus_a', :fingerprint_a, 'hashing', 2, "
            "'index_v1', 'none_v1')"
        ),
        {"fingerprint_a": "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_vectors "
            "(id, chunk_id, index_manifest_id, corpus_version_id, vector) VALUES "
            "('vector_a', 'chunk_a', 'index_a', 'corpus_a', '[0.1, 0.2]')"
        )
    )
    if scenario == "ambiguous":
        connection.execute(
            text(
                "INSERT INTO v2_knowledge_index_manifests "
                "(id, corpus_version_id, fingerprint, embedding_model, "
                "embedding_dimension, chunker_version, enrichment_policy_version) "
                "VALUES ('index_b', 'corpus_a', :fingerprint_b, 'hashing', 2, "
                "'other_v1', 'none_v1')"
            ),
            {"fingerprint_b": "b" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO v2_knowledge_vectors "
                "(id, chunk_id, index_manifest_id, corpus_version_id, vector) VALUES "
                "('vector_b', 'chunk_a', 'index_b', 'corpus_a', '[0.3, 0.4]')"
            )
        )


def _seed_head_document(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_corpus_versions "
            "(id, corpus_name, version, status) VALUES "
            "('corpus_a', 'synthetic', 'source_v1', 'draft')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_documents "
            "(id, corpus_version_id, source_url, title, use_basis, content_hash, "
            "document_version, retrieved_at) VALUES "
            "('document_a', 'corpus_a', 'https://example.test/source', "
            "'Synthetic source', 'public metadata', :content_hash, 'document_v1', "
            "'2026-09-09 00:00:00+00:00')"
        ),
        {"content_hash": "c" * 64},
    )


def _insert_chunk(
    connection: Connection,
    *,
    chunk_id: str,
    chunker_version: str,
    chunk_index: int,
) -> None:
    connection.execute(
        text(
            "INSERT INTO v2_knowledge_chunks "
            "(id, document_id, corpus_version_id, chunker_version, chunk_index, "
            "content, token_count) VALUES "
            "(:chunk_id, 'document_a', 'corpus_a', :chunker_version, :chunk_index, "
            "'Synthetic chunk', 2)"
        ),
        {
            "chunk_id": chunk_id,
            "chunker_version": chunker_version,
            "chunk_index": chunk_index,
        },
    )


def _knowledge_snapshot(connection: Connection) -> dict[str, list[tuple[object, ...]]]:
    queries = {
        "corpora": (
            "SELECT id, corpus_name, version, status, manifest, created_at, "
            "published_at FROM v2_knowledge_corpus_versions ORDER BY id"
        ),
        "indexes": (
            "SELECT id, corpus_version_id, fingerprint, embedding_model, "
            "embedding_dimension, chunker_version, enrichment_policy_version, "
            "manifest, created_at FROM v2_knowledge_index_manifests ORDER BY id"
        ),
        "documents": (
            "SELECT id, corpus_version_id, source_url, title, use_basis, content_hash, "
            "document_version, retrieved_at, access_metadata, source_metadata, "
            "created_at FROM v2_knowledge_documents ORDER BY id"
        ),
        "chunks": (
            "SELECT id, document_id, corpus_version_id, chunk_index, content, "
            "token_count, metadata, created_at FROM v2_knowledge_chunks ORDER BY id"
        ),
        "vectors": (
            "SELECT id, chunk_id, index_manifest_id, corpus_version_id, vector, "
            "created_at FROM v2_knowledge_vectors ORDER BY id"
        ),
    }
    return {
        name: [tuple(row) for row in connection.execute(text(query)).all()]
        for name, query in queries.items()
    }


def _chunker_assignments(connection: Connection) -> list[tuple[object, ...]]:
    return connection.execute(
        text("SELECT id, chunker_version FROM v2_knowledge_chunks ORDER BY id")
    ).all()


def _expected_assignments() -> list[tuple[str, str]]:
    return [
        ("chunk_both", "metadata_v1"),
        ("chunk_index", "index_v1"),
        ("chunk_legacy", _LEGACY_CHUNKER_VERSION),
        ("chunk_metadata", "metadata_v1"),
    ]


def _assert_chunker_schema(engine: Engine) -> None:
    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("v2_knowledge_chunks")
    }
    assert columns["chunker_version"]["nullable"] is False
    assert columns["chunker_version"]["type"].length == 120
    _assert_unique_constraint(
        engine,
        _NEW_UNIQUE,
        ["document_id", "chunker_version", "chunk_index"],
    )
    assert _OLD_UNIQUE not in {
        constraint["name"]
        for constraint in inspect(engine).get_unique_constraints("v2_knowledge_chunks")
    }


def _assert_unique_constraint(
    engine: Engine,
    name: str,
    columns: list[str],
) -> None:
    constraint = next(
        constraint
        for constraint in inspect(engine).get_unique_constraints("v2_knowledge_chunks")
        if constraint["name"] == name
    )
    assert constraint["column_names"] == columns


def _column_names(engine: Engine) -> set[str]:
    return {
        column["name"] for column in inspect(engine).get_columns("v2_knowledge_chunks")
    }


def _revision(connection: Connection) -> str:
    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert isinstance(revision, str)
    return revision


def _downgrade_database(engine: Engine, revision: str) -> None:
    migration_config = _migration_config()
    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            migration_config.attributes["connection"] = connection
            command.downgrade(migration_config, revision)
            connection.commit()
        return
    with engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        command.downgrade(migration_config, revision)
