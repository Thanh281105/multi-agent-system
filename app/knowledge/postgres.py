"""PostgreSQL persistence for immutable v2 knowledge builds and reads."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.knowledge.ingestion import (
    KnowledgeIngestionError,
    PreparedChunk,
    PreparedCorpusBuild,
    PreparedDocument,
    PreparedVector,
    embedding_reuse_key,
)
from app.knowledge.v2_contracts import (
    AuthorizedKnowledgeSource,
    BookMappingStatus,
    CorpusBuildResult,
    KnowledgeSpan,
    PrincipalAccessPolicy,
    PublishedKnowledgeSnapshot,
    ResolvedKnowledgeEvidence,
    RetrievalChunk,
    RetrievalPolicy,
    SourceSupportScope,
    canonical_json_sha256,
    stable_evidence_id,
)
from app.models.v2 import (
    V2BookMapping,
    V2KnowledgeChunk,
    V2KnowledgeCorpusVersion,
    V2KnowledgeDocument,
    V2KnowledgeIndexManifest,
    V2KnowledgeVector,
)
from app.v2.authorization import (
    DEMO_STORE_ID,
    AuthorityOverrideError,
    AuthorizationDeniedError,
    ResourceAuthorization,
    ResourceNotFoundError,
    required_scopes_for_mode,
)


class KnowledgeSnapshotError(LookupError):
    """A requested published corpus/index snapshot is unavailable or ambiguous."""


class PostgresKnowledgeStore:
    """Use short PostgreSQL transactions around deterministic ingestion state."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def prepare_build(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
    ) -> CorpusBuildResult | None:
        """Validate immutable identity and persist a restart-safe draft plan."""

        with self._session_factory() as session, session.begin():
            corpus = session.scalar(
                select(V2KnowledgeCorpusVersion)
                .where(
                    V2KnowledgeCorpusVersion.corpus_name == build.corpus_name,
                    V2KnowledgeCorpusVersion.version == build.corpus_version,
                )
                .with_for_update()
            )
            if corpus is None:
                corpus = V2KnowledgeCorpusVersion(
                    id=build.corpus_version_id,
                    corpus_name=build.corpus_name,
                    version=build.corpus_version,
                    status="draft",
                    manifest=dict(build.corpus_manifest),
                )
                session.add(corpus)
                session.flush()
            else:
                self._assert_corpus_matches(corpus, build)

            index = session.scalar(
                select(V2KnowledgeIndexManifest)
                .where(V2KnowledgeIndexManifest.id == build.index_manifest_id)
                .with_for_update()
            )
            if index is None:
                index = V2KnowledgeIndexManifest(
                    id=build.index_manifest_id,
                    corpus_version_id=build.corpus_version_id,
                    fingerprint=build.index_spec.index_fingerprint,
                    embedding_model=build.index_spec.embedding_model,
                    embedding_dimension=build.index_spec.embedding_dimension,
                    chunker_version=build.index_spec.chunker_version,
                    enrichment_policy_version=(
                        build.index_spec.enrichment_policy_version
                    ),
                    manifest=self._new_index_manifest(build, build_owner_id),
                )
                session.add(index)
                session.flush()
            else:
                self._assert_index_matches(index, build)
                manifest = dict(index.manifest)
                if manifest.get("complete") is True and corpus.status == "published":
                    return self._build_result(
                        session,
                        build,
                        reused_source_count=len(build.documents),
                        reused_vector_count=build.expected_vector_count,
                    )
                active_batch = manifest.get("active_embedding_batch")
                if active_batch is not None:
                    raise KnowledgeIngestionError("embedding_batch_outcome_unknown")
                existing_owner = manifest.get("build_owner_id")
                if existing_owner is not None and existing_owner != build_owner_id:
                    raise KnowledgeIngestionError(
                        "knowledge_build_owned_by_another_run"
                    )
                manifest["build_owner_id"] = build_owner_id
                index.manifest = manifest

            if build.publish:
                self._assert_current_catalog_is_complete(session, build)
            self._persist_documents_and_chunks(session, build)
            self._persist_mappings(session, build)
        return None

    def find_reusable_vectors(
        self,
        *,
        index_fingerprint: str,
        chunks: Sequence[PreparedChunk],
        embedding_dimension: int,
    ) -> Mapping[str, tuple[float, ...]]:
        wanted = {embedding_reuse_key(chunk) for chunk in chunks}
        if not wanted:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    V2KnowledgeChunk.metadata_json,
                    V2KnowledgeVector.vector,
                    V2KnowledgeIndexManifest.manifest,
                )
                .join(
                    V2KnowledgeVector,
                    and_(
                        V2KnowledgeVector.chunk_id == V2KnowledgeChunk.id,
                        V2KnowledgeVector.corpus_version_id
                        == V2KnowledgeChunk.corpus_version_id,
                    ),
                )
                .join(
                    V2KnowledgeIndexManifest,
                    and_(
                        V2KnowledgeIndexManifest.id
                        == V2KnowledgeVector.index_manifest_id,
                        V2KnowledgeIndexManifest.corpus_version_id
                        == V2KnowledgeVector.corpus_version_id,
                    ),
                )
                .where(
                    V2KnowledgeIndexManifest.fingerprint == index_fingerprint,
                    V2KnowledgeIndexManifest.embedding_dimension == embedding_dimension,
                )
            ).all()
        reusable: dict[str, tuple[float, ...]] = {}
        for metadata, raw_vector, index_manifest in rows:
            key = self._stored_reuse_key(metadata, index_manifest)
            if key not in wanted:
                continue
            vector = self._checked_vector(raw_vector, embedding_dimension)
            previous = reusable.get(key)
            if previous is not None and previous != vector:
                raise KnowledgeIngestionError("reusable embedding vectors conflict")
            reusable[key] = vector
        return reusable

    def claim_embedding_batch(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        chunks: Sequence[PreparedChunk],
    ) -> str | None:
        """Fence one provider batch without holding a lock during network work."""

        if not chunks:
            raise ValueError("embedding batch cannot be empty")
        batch_id = (
            "batch_"
            + canonical_json_sha256([embedding_reuse_key(chunk) for chunk in chunks])[
                :58
            ]
        )
        chunk_ids = tuple(chunk.chunk_row_id for chunk in chunks)
        with self._session_factory() as session, session.begin():
            index = self._locked_index(session, build.index_manifest_id)
            self._assert_index_matches(index, build)
            manifest = dict(index.manifest)
            if manifest.get("complete") is True:
                return None
            if manifest.get("build_owner_id") != build_owner_id:
                raise KnowledgeIngestionError("knowledge_build_owned_by_another_run")
            if manifest.get("active_embedding_batch") is not None:
                raise KnowledgeIngestionError("embedding_batch_outcome_unknown")
            existing_count = session.scalar(
                select(func.count())
                .select_from(V2KnowledgeVector)
                .where(
                    V2KnowledgeVector.index_manifest_id == build.index_manifest_id,
                    V2KnowledgeVector.chunk_id.in_(chunk_ids),
                )
            )
            if existing_count == len(chunk_ids):
                return None
            if existing_count:
                raise KnowledgeIngestionError("embedding_batch_is_partially_persisted")
            manifest["active_embedding_batch"] = {
                "batch_id": batch_id,
                "build_owner_id": build_owner_id,
                "chunk_row_ids": list(chunk_ids),
                "embedding_input_hashes": [
                    chunk.embedding_input_hash for chunk in chunks
                ],
                "claimed_at": datetime.now(UTC).isoformat(),
            }
            index.manifest = manifest
        return batch_id

    def persist_vector_batch(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        batch_id: str,
        vectors: Sequence[PreparedVector],
    ) -> None:
        """Checkpoint one completed provider batch and release its fence."""

        with self._session_factory() as session, session.begin():
            index = self._locked_index(session, build.index_manifest_id)
            self._assert_index_matches(index, build)
            manifest = dict(index.manifest)
            active = manifest.get("active_embedding_batch")
            if not isinstance(active, dict) or active.get("batch_id") != batch_id:
                raise KnowledgeIngestionError("embedding_batch_claim_mismatch")
            if active.get("build_owner_id") != build_owner_id:
                raise KnowledgeIngestionError("knowledge_build_owned_by_another_run")
            expected_chunk_ids = tuple(active.get("chunk_row_ids", ()))
            if set(expected_chunk_ids) != {vector.chunk_row_id for vector in vectors}:
                raise KnowledgeIngestionError("embedding_batch_vector_set_mismatch")
            for vector in vectors:
                self._persist_vector(session, build, vector)
            manifest["active_embedding_batch"] = None
            index.manifest = manifest

    def persist_reused_vectors(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        vectors: Sequence[PreparedVector],
    ) -> None:
        """Copy validated same-fingerprint vectors into this corpus atomically."""

        if not vectors:
            return
        with self._session_factory() as session, session.begin():
            index = self._locked_index(session, build.index_manifest_id)
            self._assert_index_matches(index, build)
            manifest = dict(index.manifest)
            if manifest.get("complete") is not True and (
                manifest.get("build_owner_id") != build_owner_id
            ):
                raise KnowledgeIngestionError("knowledge_build_owned_by_another_run")
            if manifest.get("active_embedding_batch") is not None:
                raise KnowledgeIngestionError("embedding_batch_outcome_unknown")
            for vector in vectors:
                self._persist_vector(session, build, vector)

    def finalize_build(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        reused_source_count: int,
        reused_vector_count: int,
    ) -> CorpusBuildResult:
        """Validate completeness and atomically publish the immutable snapshot."""

        with self._session_factory() as session, session.begin():
            corpus = session.scalar(
                select(V2KnowledgeCorpusVersion)
                .where(V2KnowledgeCorpusVersion.id == build.corpus_version_id)
                .with_for_update()
            )
            if corpus is None:
                raise KnowledgeIngestionError("knowledge corpus draft is missing")
            self._assert_corpus_matches(corpus, build)
            index = self._locked_index(session, build.index_manifest_id)
            self._assert_index_matches(index, build)
            manifest = dict(index.manifest)
            if manifest.get("active_embedding_batch") is not None:
                raise KnowledgeIngestionError("embedding_batch_outcome_unknown")
            if manifest.get("complete") is not True and (
                manifest.get("build_owner_id") != build_owner_id
            ):
                raise KnowledgeIngestionError("knowledge_build_owned_by_another_run")
            self._assert_build_counts(session, build)
            if build.publish:
                self._assert_current_catalog_is_complete(session, build)
            manifest["complete"] = True
            manifest["completed_at"] = datetime.now(UTC).isoformat()
            index.manifest = manifest
            if build.publish and corpus.status != "published":
                corpus.status = "published"
                corpus.published_at = datetime.now(UTC)
            result = self._build_result(
                session,
                build,
                reused_source_count=reused_source_count,
                reused_vector_count=reused_vector_count,
            )
        return result

    def resolve_published_snapshot(
        self,
        corpus_version_id: str,
        *,
        index_manifest_id: str | None = None,
    ) -> PublishedKnowledgeSnapshot:
        with self._session_factory() as session:
            corpus = session.get(V2KnowledgeCorpusVersion, corpus_version_id)
            if (
                corpus is None
                or corpus.status != "published"
                or corpus.published_at is None
            ):
                raise KnowledgeSnapshotError("published knowledge snapshot not found")
            indexes = session.scalars(
                select(V2KnowledgeIndexManifest).where(
                    V2KnowledgeIndexManifest.corpus_version_id == corpus_version_id
                )
            ).all()
            complete = [
                item for item in indexes if dict(item.manifest).get("complete") is True
            ]
            if index_manifest_id is None:
                if len(complete) != 1:
                    raise KnowledgeSnapshotError(
                        "index manifest is required when completeness is ambiguous"
                    )
                index = complete[0]
            else:
                matches = [item for item in complete if item.id == index_manifest_id]
                if len(matches) != 1:
                    raise KnowledgeSnapshotError("published knowledge index not found")
                index = matches[0]
            expected_vectors = int(dict(index.manifest).get("vector_count", -1))
            vector_count = session.scalar(
                select(func.count())
                .select_from(V2KnowledgeVector)
                .where(V2KnowledgeVector.index_manifest_id == index.id)
            )
            if vector_count != expected_vectors:
                raise KnowledgeSnapshotError("published knowledge index is incomplete")
            retrieval_policy = RetrievalPolicy.model_validate(
                dict(corpus.manifest).get("retrieval_policy")
            )
            return PublishedKnowledgeSnapshot(
                corpus_version_id=corpus.id,
                corpus_name=corpus.corpus_name,
                corpus_version=corpus.version,
                index_manifest_id=index.id,
                index_fingerprint=index.fingerprint,
                embedding_model=index.embedding_model,
                embedding_dimension=index.embedding_dimension,
                chunker_version=index.chunker_version,
                enrichment_policy_version=index.enrichment_policy_version,
                query_embedding_fingerprint=str(
                    dict(index.manifest)["query_embedding_fingerprint"]
                ),
                retrieval_policy=retrieval_policy,
                published_at=corpus.published_at,
            )

    def list_authorized_sources(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        product_ids: Sequence[int] = (),
    ) -> tuple[AuthorizedKnowledgeSource, ...]:
        self._assert_access_context(access)
        with self._session_factory() as session:
            documents = session.scalars(
                self._authorized_documents_query(snapshot, access).order_by(
                    V2KnowledgeDocument.id
                )
            ).all()
            if product_ids:
                mappings = session.scalars(
                    select(V2BookMapping).where(
                        V2BookMapping.corpus_version_id == snapshot.corpus_version_id,
                        V2BookMapping.product_id.in_(tuple(product_ids)),
                        V2BookMapping.mapping_status.in_(
                            (
                                BookMappingStatus.EXACT_WORK.value,
                                BookMappingStatus.EXACT_EDITION.value,
                            )
                        ),
                    )
                ).all()
                mapped_source_ids = {
                    source_id
                    for mapping in mappings
                    for source_id in mapping.mapping_metadata.get("source_ids", ())
                }
                documents = [
                    document
                    for document in documents
                    if document.source_metadata.get("source_id") in mapped_source_ids
                ]
            return tuple(self._authorized_source(document) for document in documents)

    def load_authorized_chunks(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        source_ids: frozenset[str],
    ) -> tuple[RetrievalChunk, ...]:
        self._assert_access_context(access)
        if not source_ids:
            return ()
        source_expression = cast(V2KnowledgeDocument.source_metadata, JSONB)[
            "source_id"
        ].as_string()
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    V2KnowledgeDocument,
                    V2KnowledgeChunk,
                    V2KnowledgeVector,
                )
                .join(
                    V2KnowledgeChunk,
                    and_(
                        V2KnowledgeChunk.document_id == V2KnowledgeDocument.id,
                        V2KnowledgeChunk.corpus_version_id
                        == V2KnowledgeDocument.corpus_version_id,
                    ),
                )
                .join(
                    V2KnowledgeVector,
                    and_(
                        V2KnowledgeVector.chunk_id == V2KnowledgeChunk.id,
                        V2KnowledgeVector.corpus_version_id
                        == V2KnowledgeChunk.corpus_version_id,
                        V2KnowledgeVector.index_manifest_id
                        == snapshot.index_manifest_id,
                    ),
                )
                .where(
                    *self._authorized_document_conditions(snapshot, access),
                    source_expression.in_(tuple(source_ids)),
                )
                .order_by(V2KnowledgeDocument.id, V2KnowledgeChunk.chunk_index)
            ).all()
        found_source_ids = {
            str(document.source_metadata.get("source_id")) for document, _, _ in rows
        }
        if found_source_ids != set(source_ids):
            raise AuthorityOverrideError
        return tuple(
            self._retrieval_chunk(snapshot, document, chunk, vector)
            for document, chunk, vector in rows
        )

    def resolve_authorized_evidence(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        source_id: str,
        source_version_id: str,
        chunk_id: str,
        span_id: str,
    ) -> ResolvedKnowledgeEvidence:
        self._assert_access_context(access)
        source_metadata = cast(V2KnowledgeDocument.source_metadata, JSONB)
        chunk_metadata = cast(V2KnowledgeChunk.metadata_json, JSONB)
        with self._session_factory() as session:
            row = session.execute(
                select(
                    V2KnowledgeDocument,
                    V2KnowledgeChunk,
                    V2KnowledgeVector,
                )
                .join(
                    V2KnowledgeChunk,
                    and_(
                        V2KnowledgeChunk.document_id == V2KnowledgeDocument.id,
                        V2KnowledgeChunk.corpus_version_id
                        == V2KnowledgeDocument.corpus_version_id,
                    ),
                )
                .join(
                    V2KnowledgeVector,
                    and_(
                        V2KnowledgeVector.chunk_id == V2KnowledgeChunk.id,
                        V2KnowledgeVector.corpus_version_id
                        == V2KnowledgeChunk.corpus_version_id,
                        V2KnowledgeVector.index_manifest_id
                        == snapshot.index_manifest_id,
                    ),
                )
                .where(
                    *self._authorized_document_conditions(snapshot, access),
                    source_metadata["source_id"].as_string() == source_id,
                    source_metadata["source_version_id"].as_string()
                    == source_version_id,
                    chunk_metadata["public_chunk_id"].as_string() == chunk_id,
                )
            ).one_or_none()
        if row is None:
            raise ResourceNotFoundError
        document, chunk, vector = row
        checked_chunk = self._retrieval_chunk(snapshot, document, chunk, vector)
        matching_spans = [
            span for span in checked_chunk.spans if span.span_id == span_id
        ]
        if len(matching_spans) != 1:
            raise ResourceNotFoundError
        span = matching_spans[0]
        excerpt = checked_chunk.content[span.start_char : span.end_char]
        return ResolvedKnowledgeEvidence.model_validate(
            {
                "evidence_id": stable_evidence_id(
                    source_id=source_id,
                    source_version_id=source_version_id,
                    chunk_id=chunk_id,
                    span_id=span_id,
                ),
                "corpus_version_id": snapshot.corpus_version_id,
                "source_id": source_id,
                "source_version_id": source_version_id,
                "chunk_id": chunk_id,
                "span_id": span_id,
                "title": document.title,
                "url": document.source_url,
                "excerpt": excerpt,
                "content_hash": span.content_hash,
                "observed_at": document.retrieved_at,
            }
        )

    @staticmethod
    def _new_index_manifest(
        build: PreparedCorpusBuild, build_owner_id: str
    ) -> dict[str, Any]:
        return {
            **dict(build.index_manifest),
            "build_owner_id": build_owner_id,
            "active_embedding_batch": None,
            "complete": False,
        }

    @staticmethod
    def _assert_corpus_matches(
        corpus: V2KnowledgeCorpusVersion, build: PreparedCorpusBuild
    ) -> None:
        if (
            corpus.id != build.corpus_version_id
            or corpus.corpus_name != build.corpus_name
            or corpus.version != build.corpus_version
            or corpus.manifest != dict(build.corpus_manifest)
        ):
            raise KnowledgeIngestionError(
                "corpus name/version immutable identity conflict"
            )

    @staticmethod
    def _assert_index_matches(
        index: V2KnowledgeIndexManifest, build: PreparedCorpusBuild
    ) -> None:
        expected = (
            build.corpus_version_id,
            build.index_spec.index_fingerprint,
            build.index_spec.embedding_model,
            build.index_spec.embedding_dimension,
            build.index_spec.chunker_version,
            build.index_spec.enrichment_policy_version,
        )
        actual = (
            index.corpus_version_id,
            index.fingerprint,
            index.embedding_model,
            index.embedding_dimension,
            index.chunker_version,
            index.enrichment_policy_version,
        )
        manifest = dict(index.manifest)
        static_manifest = {key: manifest.get(key) for key in build.index_manifest}
        if actual != expected or static_manifest != dict(build.index_manifest):
            raise KnowledgeIngestionError("knowledge index immutable identity conflict")

    def _persist_documents_and_chunks(
        self, session: Session, build: PreparedCorpusBuild
    ) -> None:
        for document in build.documents:
            access_metadata = document.source.access.model_dump(mode="json")
            source_metadata = self._source_metadata(document)
            existing = session.get(V2KnowledgeDocument, document.document_row_id)
            expected_document = (
                build.corpus_version_id,
                str(document.source.source_url),
                document.source.title,
                document.source.use_basis,
                document.source.content_sha256,
                document.source.document_version,
                self._utc(document.source.retrieved_at),
                access_metadata,
                source_metadata,
            )
            if existing is None:
                session.add(
                    V2KnowledgeDocument(
                        id=document.document_row_id,
                        corpus_version_id=build.corpus_version_id,
                        source_url=str(document.source.source_url),
                        title=document.source.title,
                        use_basis=document.source.use_basis,
                        content_hash=document.source.content_sha256,
                        document_version=document.source.document_version,
                        retrieved_at=document.source.retrieved_at,
                        access_metadata=access_metadata,
                        source_metadata=source_metadata,
                    )
                )
                session.flush()
            else:
                actual_document = (
                    existing.corpus_version_id,
                    existing.source_url,
                    existing.title,
                    existing.use_basis,
                    existing.content_hash,
                    existing.document_version,
                    self._utc(existing.retrieved_at),
                    existing.access_metadata,
                    existing.source_metadata,
                )
                if actual_document != expected_document:
                    raise KnowledgeIngestionError(
                        "knowledge document identity conflict"
                    )
            for chunk in document.chunks:
                self._persist_chunk(session, build, chunk)

    def _persist_chunk(
        self, session: Session, build: PreparedCorpusBuild, chunk: PreparedChunk
    ) -> None:
        metadata = {
            "schema_version": "1.0",
            "public_chunk_id": chunk.public_chunk_id,
            "chunker_version": build.index_spec.chunker_version,
            "content_hash": chunk.content_hash,
            "spans": [span.model_dump(mode="json") for span in chunk.spans],
        }
        existing = session.get(V2KnowledgeChunk, chunk.chunk_row_id)
        expected = (
            chunk.document_row_id,
            build.corpus_version_id,
            chunk.chunk_index,
            build.index_spec.chunker_version,
            chunk.content,
            chunk.token_count,
            metadata,
        )
        if existing is None:
            session.add(
                V2KnowledgeChunk(
                    id=chunk.chunk_row_id,
                    document_id=chunk.document_row_id,
                    corpus_version_id=build.corpus_version_id,
                    chunk_index=chunk.chunk_index,
                    chunker_version=build.index_spec.chunker_version,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    metadata_json=metadata,
                )
            )
            session.flush()
            return
        actual = (
            existing.document_id,
            existing.corpus_version_id,
            existing.chunk_index,
            existing.chunker_version,
            existing.content,
            existing.token_count,
            existing.metadata_json,
        )
        if actual != expected:
            raise KnowledgeIngestionError("knowledge chunk identity conflict")

    @staticmethod
    def _persist_mappings(session: Session, build: PreparedCorpusBuild) -> None:
        for prepared in build.mappings:
            mapping = prepared.mapping
            metadata = {
                "schema_version": "1.0",
                "source_ids": list(mapping.source_ids),
                "mapping_evidence": mapping.mapping_evidence,
                "ambiguity_reason": mapping.ambiguity_reason,
                "unmatched_reason": mapping.unmatched_reason,
            }
            existing = session.get(V2BookMapping, prepared.mapping_row_id)
            expected = (
                build.corpus_version_id,
                mapping.product_id,
                mapping.mapping_status.value,
                mapping.edition_identifier,
                mapping.work_identifier,
                metadata,
            )
            if existing is None:
                session.add(
                    V2BookMapping(
                        id=prepared.mapping_row_id,
                        corpus_version_id=build.corpus_version_id,
                        product_id=mapping.product_id,
                        mapping_status=mapping.mapping_status.value,
                        edition_identifier=mapping.edition_identifier,
                        work_identifier=mapping.work_identifier,
                        mapping_metadata=metadata,
                    )
                )
                continue
            actual = (
                existing.corpus_version_id,
                existing.product_id,
                existing.mapping_status,
                existing.edition_identifier,
                existing.work_identifier,
                existing.mapping_metadata,
            )
            if actual != expected:
                raise KnowledgeIngestionError("book mapping identity conflict")

    @staticmethod
    def _persist_vector(
        session: Session, build: PreparedCorpusBuild, vector: PreparedVector
    ) -> None:
        input_hashes = build.index_manifest.get("embedding_input_hashes", {})
        if (
            not isinstance(input_hashes, dict)
            or input_hashes.get(vector.public_chunk_id) != vector.embedding_input_hash
        ):
            raise KnowledgeIngestionError(
                "vector input hash does not match its immutable index manifest"
            )
        existing = session.get(V2KnowledgeVector, vector.vector_row_id)
        expected = (
            vector.chunk_row_id,
            build.index_manifest_id,
            build.corpus_version_id,
            list(vector.vector),
        )
        if existing is None:
            session.add(
                V2KnowledgeVector(
                    id=vector.vector_row_id,
                    chunk_id=vector.chunk_row_id,
                    index_manifest_id=build.index_manifest_id,
                    corpus_version_id=build.corpus_version_id,
                    vector=list(vector.vector),
                )
            )
            session.flush()
            return
        actual = (
            existing.chunk_id,
            existing.index_manifest_id,
            existing.corpus_version_id,
            existing.vector,
        )
        if actual != expected:
            raise KnowledgeIngestionError("knowledge vector identity conflict")

    @staticmethod
    def _source_metadata(document: PreparedDocument) -> dict[str, Any]:
        source = document.source
        planning_text = "\n".join(
            part
            for part in (
                source.title,
                " ".join(source.keywords),
                source.content_markdown,
            )
            if part
        )[:2_000].strip()
        return {
            "schema_version": "1.0",
            "source_id": source.source_id,
            "source_version_id": source.source_version_id,
            "source_kind": source.source_kind.value,
            "language": source.language,
            "support_scope": source.support_scope.value,
            "work_identifier": source.work_identifier,
            "edition_identifier": source.edition_identifier,
            "keywords": list(source.keywords),
            "metadata": source.metadata.model_dump(mode="json"),
            "planning_text": planning_text,
        }

    @staticmethod
    def _stored_reuse_key(
        chunk_metadata: Mapping[str, Any],
        index_manifest: Mapping[str, Any],
    ) -> str:
        public_chunk_id = str(chunk_metadata.get("public_chunk_id"))
        hashes = index_manifest.get("embedding_input_hashes", {})
        if not isinstance(hashes, dict):
            raise KnowledgeIngestionError(
                "knowledge index embedding input hashes are invalid"
            )
        input_hash = hashes.get(public_chunk_id)
        if not isinstance(input_hash, str):
            raise KnowledgeIngestionError(
                "knowledge index is missing an embedding input hash"
            )
        return f"{public_chunk_id}:{input_hash}"

    @staticmethod
    def _checked_vector(raw: Sequence[float], dimension: int) -> tuple[float, ...]:
        vector = tuple(float(component) for component in raw)
        if len(vector) != dimension or any(
            not math.isfinite(component) for component in vector
        ):
            raise KnowledgeIngestionError("stored embedding vector is incompatible")
        return vector

    @staticmethod
    def _locked_index(
        session: Session, index_manifest_id: str
    ) -> V2KnowledgeIndexManifest:
        index = session.scalar(
            select(V2KnowledgeIndexManifest)
            .where(V2KnowledgeIndexManifest.id == index_manifest_id)
            .with_for_update()
        )
        if index is None:
            raise KnowledgeIngestionError("knowledge index draft is missing")
        return index

    @staticmethod
    def _assert_build_counts(session: Session, build: PreparedCorpusBuild) -> None:
        document_count = session.scalar(
            select(func.count())
            .select_from(V2KnowledgeDocument)
            .where(V2KnowledgeDocument.corpus_version_id == build.corpus_version_id)
        )
        chunk_count = session.scalar(
            select(func.count())
            .select_from(V2KnowledgeChunk)
            .where(
                V2KnowledgeChunk.corpus_version_id == build.corpus_version_id,
                V2KnowledgeChunk.chunker_version == build.index_spec.chunker_version,
            )
        )
        vector_count = session.scalar(
            select(func.count())
            .select_from(V2KnowledgeVector)
            .where(V2KnowledgeVector.index_manifest_id == build.index_manifest_id)
        )
        mapping_count = session.scalar(
            select(func.count())
            .select_from(V2BookMapping)
            .where(V2BookMapping.corpus_version_id == build.corpus_version_id)
        )
        if (
            document_count != len(build.documents)
            or chunk_count != build.expected_vector_count
            or vector_count != build.expected_vector_count
            or mapping_count != len(build.mappings)
        ):
            raise KnowledgeIngestionError("knowledge build is incomplete")

    @staticmethod
    def _assert_current_catalog_is_complete(
        session: Session, build: PreparedCorpusBuild
    ) -> None:
        database_ids = {
            int(value)
            for value in session.scalars(text("SELECT id FROM products")).all()
        }
        mapped_ids = {prepared.mapping.product_id for prepared in build.mappings}
        if len(database_ids) != 200 or mapped_ids != database_ids:
            raise KnowledgeIngestionError(
                "publication mappings do not match the current 200-book catalog"
            )

    def _build_result(
        self,
        session: Session,
        build: PreparedCorpusBuild,
        *,
        reused_source_count: int,
        reused_vector_count: int,
    ) -> CorpusBuildResult:
        corpus = session.get(V2KnowledgeCorpusVersion, build.corpus_version_id)
        return CorpusBuildResult(
            corpus_version_id=build.corpus_version_id,
            index_manifest_id=build.index_manifest_id,
            index_fingerprint=build.index_spec.index_fingerprint,
            source_count=len(build.documents),
            chunk_count=build.expected_vector_count,
            vector_count=build.expected_vector_count,
            mapping_count=len(build.mappings),
            reused_source_count=reused_source_count,
            reused_vector_count=reused_vector_count,
            published=bool(corpus is not None and corpus.status == "published"),
        )

    def _authorized_documents_query(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
    ) -> Select[tuple[V2KnowledgeDocument]]:
        return select(V2KnowledgeDocument).where(
            *self._authorized_document_conditions(snapshot, access)
        )

    @staticmethod
    def _authorized_document_conditions(
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
    ) -> tuple[Any, ...]:
        metadata = cast(V2KnowledgeDocument.access_metadata, JSONB)
        tenant_ids = metadata["allowed_tenant_ids"]
        principal_policy = metadata["principal_policy"].as_string()
        allowed_principals = metadata["allowed_principal_ids"]
        mode_key = (
            "shopper_required_scopes"
            if access.binding.mode.value == "shopper"
            else "merchant_required_scopes"
        )
        required_scopes = metadata[mode_key]
        return (
            V2KnowledgeDocument.corpus_version_id == snapshot.corpus_version_id,
            tenant_ids.contains([access.binding.tenant_id]),
            or_(
                principal_policy == PrincipalAccessPolicy.AUTHENTICATED.value,
                and_(
                    principal_policy == PrincipalAccessPolicy.ALLOWLIST.value,
                    allowed_principals.contains([access.binding.principal_id]),
                ),
            ),
            required_scopes.is_not(None),
            required_scopes.contained_by(sorted(access.scopes)),
        )

    @staticmethod
    def _assert_access_context(access: ResourceAuthorization) -> None:
        if access.binding.store_id != DEMO_STORE_ID:
            raise ResourceNotFoundError
        if not required_scopes_for_mode(access.binding.mode) <= access.scopes:
            raise AuthorizationDeniedError

    @staticmethod
    def _authorized_source(
        document: V2KnowledgeDocument,
    ) -> AuthorizedKnowledgeSource:
        metadata = document.source_metadata
        return AuthorizedKnowledgeSource.model_validate(
            {
                "source_id": metadata["source_id"],
                "source_version_id": metadata["source_version_id"],
                "title": document.title,
                "url": document.source_url,
                "planning_text": metadata["planning_text"],
                "keywords": tuple(metadata.get("keywords", ())),
                "support_scope": SourceSupportScope(metadata["support_scope"]),
                "work_identifier": metadata.get("work_identifier"),
                "edition_identifier": metadata.get("edition_identifier"),
                "retrieved_at": document.retrieved_at,
            }
        )

    def _retrieval_chunk(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        document: V2KnowledgeDocument,
        chunk: V2KnowledgeChunk,
        vector: V2KnowledgeVector,
    ) -> RetrievalChunk:
        source_metadata = document.source_metadata
        chunk_metadata = chunk.metadata_json
        if chunk.chunker_version != snapshot.chunker_version:
            raise KnowledgeSnapshotError("chunk belongs to a different chunker")
        spans = tuple(
            KnowledgeSpan.model_validate(item)
            for item in chunk_metadata.get("spans", ())
        )
        return RetrievalChunk.model_validate(
            {
                "corpus_version_id": snapshot.corpus_version_id,
                "index_manifest_id": snapshot.index_manifest_id,
                "source_id": source_metadata["source_id"],
                "source_version_id": source_metadata["source_version_id"],
                "chunk_id": chunk_metadata["public_chunk_id"],
                "chunk_index": chunk.chunk_index,
                "chunker_version": chunk.chunker_version,
                "title": document.title,
                "url": document.source_url,
                "content": chunk.content,
                "token_count": chunk.token_count,
                "content_hash": chunk_metadata["content_hash"],
                "vector": self._checked_vector(
                    vector.vector, snapshot.embedding_dimension
                ),
                "spans": spans,
                "support_scope": SourceSupportScope(source_metadata["support_scope"]),
                "work_identifier": source_metadata.get("work_identifier"),
                "edition_identifier": source_metadata.get("edition_identifier"),
            }
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise KnowledgeIngestionError("knowledge timestamps must be timezone-aware")
        return value.astimezone(UTC)
