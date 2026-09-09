"""Deterministic and negative coverage for v2 knowledge ingestion contracts."""

from __future__ import annotations

import hashlib
import runpy
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.knowledge.ingestion import (
    CorpusIngestor,
    KnowledgeIngestionError,
    PreparedChunk,
    PreparedCorpusBuild,
    PreparedVector,
    TableAwareChunker,
    embedding_reuse_key,
)
from app.knowledge.v2_contracts import (
    BookMapping,
    BookMappingManifest,
    BookMappingStatus,
    CorpusBuildResult,
    CuratedSource,
    IndexBuildSpec,
    KnowledgeSpan,
    PrincipalAccessPolicy,
    RetrievalChunk,
    RetrievalPolicy,
    SourceAccessMetadata,
    SourceKind,
    SourceManifest,
    SourceSupportScope,
    content_addressed_id,
    normalize_source_content,
    sha256_text,
    sha256_utf8,
    stable_chunk_id,
    stable_span_id,
)
from app.shared.budget import conservative_text_token_bound


def _access(**updates: object) -> SourceAccessMetadata:
    payload: dict[str, object] = {
        "allowed_tenant_ids": ("default",),
        "principal_policy": PrincipalAccessPolicy.AUTHENTICATED,
        "allowed_principal_ids": (),
        "shopper_required_scopes": ("ecommerce.read",),
        "merchant_required_scopes": ("ecommerce.read", "merchant.read"),
    }
    payload.update(updates)
    return SourceAccessMetadata.model_validate(payload)


def _source(**updates: object) -> CuratedSource:
    content = "# Sapiens\r\n\r\n  Lịch sử loài người.   \r\n"
    payload: dict[str, object] = {
        "source_id": "src_sapiens_author",
        "title": "Sapiens",
        "source_url": "https://example.test/sapiens",
        "source_kind": SourceKind.AUTHOR_SITE,
        "use_basis": "Public factual work metadata.",
        "retrieved_at": datetime(2026, 9, 9, tzinfo=UTC),
        "document_version": "2026-09-09",
        "language": "vi",
        "content_markdown": content,
        "content_sha256": sha256_text(content),
        "support_scope": SourceSupportScope.WORK,
        "work_identifier": "work_sapiens_harari",
        "edition_identifier": None,
        "access": _access(),
    }
    payload.update(updates)
    return CuratedSource.model_validate(payload)


def _retrieval_chunk(**updates: object) -> RetrievalChunk:
    content = "Lịch sử loài người."
    content_hash = sha256_text(content)
    source_version_id = content_addressed_id("svr", {"source": "sapiens"})
    chunk_id = stable_chunk_id(
        source_version_id=source_version_id,
        chunker_version="table_chunker_v1",
        chunk_index=0,
        content_hash=content_hash,
    )
    span_hash = sha256_utf8(content)
    span = KnowledgeSpan(
        span_id=stable_span_id(
            chunk_id=chunk_id,
            start_char=0,
            end_char=len(content),
            content_hash=span_hash,
        ),
        start_char=0,
        end_char=len(content),
        content_hash=span_hash,
    )
    payload: dict[str, object] = {
        "corpus_version_id": content_addressed_id("cor", {"version": "1"}),
        "index_manifest_id": content_addressed_id("idx", {"index": "1"}),
        "source_id": "src_sapiens_author",
        "source_version_id": source_version_id,
        "chunk_id": chunk_id,
        "chunk_index": 0,
        "chunker_version": "table_chunker_v1",
        "title": "Sapiens",
        "url": "https://example.test/sapiens",
        "content": content,
        "token_count": 4,
        "content_hash": content_hash,
        "vector": tuple(0.0 for _ in range(32)),
        "spans": (span,),
        "support_scope": SourceSupportScope.WORK,
        "work_identifier": "work_sapiens_harari",
    }
    payload.update(updates)
    return RetrievalChunk.model_validate(payload)


def test_source_normalization_and_version_identity_are_reproducible() -> None:
    first = _source()
    second = _source(
        content_markdown="# Sapiens\n\n  Lịch sử loài người.\n",
    )

    assert first.content_markdown == normalize_source_content(second.content_markdown)
    assert first.content_sha256 == second.content_sha256
    assert first.source_version_id == second.source_version_id


def test_source_rejects_swapped_work_and_edition_identifiers() -> None:
    with pytest.raises(ValidationError, match="work_identifier"):
        _source(work_identifier="edition_sapiens_vi")

    with pytest.raises(ValidationError, match="edition_identifier"):
        _source(
            support_scope=SourceSupportScope.EDITION,
            work_identifier=None,
            edition_identifier="work_sapiens_harari",
        )


def test_source_access_rejects_blank_or_incomplete_authority() -> None:
    with pytest.raises(ValidationError, match="allowed_tenant_ids"):
        _access(allowed_tenant_ids=("   ",))

    with pytest.raises(ValidationError, match="at least one principal"):
        _access(
            principal_policy=PrincipalAccessPolicy.ALLOWLIST,
            allowed_principal_ids=(),
        )

    with pytest.raises(ValidationError, match="merchant.read and ecommerce.read"):
        _access(merchant_required_scopes=("ecommerce.read",))


def test_retrieval_chunk_rejects_non_finite_vectors_and_tampered_identity() -> None:
    with pytest.raises(ValidationError, match="must be finite"):
        _retrieval_chunk(vector=(float("nan"), *tuple(0.0 for _ in range(31))))

    with pytest.raises(ValidationError, match="chunk ID"):
        _retrieval_chunk(chunk_id=content_addressed_id("chk", {"tampered": True}))

    valid = _retrieval_chunk()
    bad_span = valid.spans[0].model_copy(update={"content_hash": "0" * 64})
    with pytest.raises(ValidationError, match="span content hash"):
        _retrieval_chunk(spans=(bad_span,))


class _HashingEmbedder:
    model = "hashing-test-v1"
    method = model
    dimensions = 32

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(tuple(texts))
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [float(value) / 255.0 for value in digest]


class _CheckpointStore:
    def __init__(self, *, interrupt_before_claim: int | None = None) -> None:
        self.build: PreparedCorpusBuild | None = None
        self.build_owner_id: str | None = None
        self.vectors: dict[str, tuple[float, ...]] = {}
        self.active_batch: str | None = None
        self.claim_count = 0
        self.interrupt_before_claim = interrupt_before_claim
        self.published = False

    def prepare_build(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
    ) -> CorpusBuildResult | None:
        if self.published:
            return self._result(
                build, len(build.documents), build.expected_vector_count
            )
        if self.active_batch is not None:
            raise KnowledgeIngestionError("embedding_batch_outcome_unknown")
        if self.build is None:
            self.build = build
            self.build_owner_id = build_owner_id
        elif self.build.corpus_version_id != build.corpus_version_id:
            raise KnowledgeIngestionError(
                "corpus name/version immutable identity conflict"
            )
        elif self.build_owner_id != build_owner_id:
            raise KnowledgeIngestionError("knowledge_build_owned_by_another_run")
        return None

    def find_reusable_vectors(
        self,
        *,
        index_fingerprint: str,
        chunks: Sequence[PreparedChunk],
        embedding_dimension: int,
    ) -> Mapping[str, tuple[float, ...]]:
        del index_fingerprint, embedding_dimension
        return {
            embedding_reuse_key(chunk): self.vectors[embedding_reuse_key(chunk)]
            for chunk in chunks
            if embedding_reuse_key(chunk) in self.vectors
        }

    def claim_embedding_batch(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        chunks: Sequence[PreparedChunk],
    ) -> str | None:
        del build, build_owner_id
        self.claim_count += 1
        if self.interrupt_before_claim == self.claim_count:
            raise RuntimeError("synthetic interruption before claim")
        self.active_batch = f"batch_{self.claim_count}"
        self._active_chunks = tuple(chunks)
        return self.active_batch

    def persist_vector_batch(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        batch_id: str,
        vectors: Sequence[PreparedVector],
    ) -> None:
        del build, build_owner_id
        assert batch_id == self.active_batch
        for chunk, vector in zip(self._active_chunks, vectors, strict=True):
            self.vectors[embedding_reuse_key(chunk)] = vector.vector
        self.active_batch = None

    def persist_reused_vectors(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        vectors: Sequence[PreparedVector],
    ) -> None:
        del build, build_owner_id
        chunk_by_id = {
            chunk.chunk_row_id: chunk
            for document in self.build.documents  # type: ignore[union-attr]
            for chunk in document.chunks
        }
        for vector in vectors:
            chunk = chunk_by_id[vector.chunk_row_id]
            self.vectors[embedding_reuse_key(chunk)] = vector.vector

    def finalize_build(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        reused_source_count: int,
        reused_vector_count: int,
    ) -> CorpusBuildResult:
        del build_owner_id
        assert len(self.vectors) == build.expected_vector_count
        self.published = build.publish
        return self._result(build, reused_source_count, reused_vector_count)

    def _result(
        self,
        build: PreparedCorpusBuild,
        reused_source_count: int,
        reused_vector_count: int,
    ) -> CorpusBuildResult:
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
            published=self.published,
        )


def _manifests(
    *, content_suffix: str = ""
) -> tuple[SourceManifest, BookMappingManifest]:
    sources: list[CuratedSource] = []
    mappings: list[BookMapping] = []
    for index in range(1, 21):
        content = f"# Work {index}\n\nPublic factual note {index}. {content_suffix}\n"
        source = _source(
            source_id=f"src_work_{index}",
            title=f"Work {index}",
            source_url=f"https://example.test/work-{index}",
            content_markdown=content,
            content_sha256=sha256_text(content),
            work_identifier=f"work_example_{index}",
            keywords=(f"keyword-{index}",),
        )
        sources.append(source)
        mappings.append(
            BookMapping(
                product_id=index,
                mapping_status=BookMappingStatus.EXACT_WORK,
                source_ids=(source.source_id,),
                work_identifier=source.work_identifier,
                mapping_evidence="Synthetic public mapping fixture.",
            )
        )
    mappings.extend(
        BookMapping(
            product_id=index,
            mapping_status=BookMappingStatus.UNMATCHED,
            mapping_evidence="Synthetic unmatched fixture.",
            unmatched_reason="No approved source in the synthetic fixture.",
        )
        for index in range(21, 201)
    )
    common: dict[str, Any] = {
        "corpus_name": "books_test",
        "corpus_version": "2026-09-09",
        "catalog_source_commit": "a" * 40,
    }
    return (
        SourceManifest(**common, sources=tuple(sources)),
        BookMappingManifest(
            **common,
            catalog_book_count=200,
            mappings=tuple(mappings),
        ),
    )


def _policy(**updates: object) -> RetrievalPolicy:
    payload: dict[str, object] = {
        "version": "policy_v1",
        "dense_weight": 1.0,
        "lexical_weight": 1.0,
        "rrf_k": 60,
        "candidate_limit": 30,
        "default_limit": 6,
        "max_limit": 8,
        "max_context_tokens": 6_000,
        "min_dense_relevance": 0.2,
        "min_lexical_coverage": 0.1,
    }
    payload.update(updates)
    return RetrievalPolicy.model_validate(payload)


def _ingestor(
    store: _CheckpointStore,
    embedder: _HashingEmbedder,
    *,
    owner: str = "build_owner_test",
    batch_size: int = 8,
    policy: RetrievalPolicy | None = None,
    enrichment_policy_version: str = "source_keywords_v1",
) -> CorpusIngestor:
    return CorpusIngestor(
        store=store,
        embedder=embedder,
        index_spec=IndexBuildSpec(
            embedding_model=embedder.model,
            embedding_dimension=embedder.dimensions,
            chunker_version="table_chunker_v1",
            enrichment_policy_version=enrichment_policy_version,
        ),
        retrieval_policy=policy or _policy(),
        embedding_batch_size=batch_size,
        build_owner_id=owner,
    )


def test_table_chunker_repeats_header_and_preserves_oversized_row_cells() -> None:
    chunker = TableAwareChunker(max_chars=220, overlap_chars=20, max_tokens=220)
    first_cell = "A" * 250
    second_cell = "B" * 250
    table = f"| Name | Notes |\n| --- | --- |\n| {first_cell} | {second_cell} |"

    chunks = chunker.split(table)

    assert len(chunks) > 2
    assert all(chunk.startswith("| Name | Notes |\n| --- | --- |") for chunk in chunks)
    assert all(len(chunk) <= 220 for chunk in chunks)
    assert all(conservative_text_token_bound(chunk) <= 220 for chunk in chunks)
    rendered_cells = [
        chunk.splitlines()[-1].strip().strip("|").split("|") for chunk in chunks
    ]
    assert "".join(cells[0].strip() for cells in rendered_cells) == first_cell
    assert "".join(cells[1].strip() for cells in rendered_cells) == second_cell


def test_chunker_enforces_utf8_bound_and_safe_sentence_overlap() -> None:
    chunker = TableAwareChunker(max_chars=240, overlap_chars=80, max_tokens=200)
    text = ("🙂" * 80) + ". " + ("x" * 199) + "."

    chunks = chunker.split(text)

    assert len(chunks) >= 3
    assert all(len(chunk) <= 240 for chunk in chunks)
    assert all(conservative_text_token_bound(chunk) <= 200 for chunk in chunks)

    with pytest.raises(ValueError, match="between 200 and 1800"):
        TableAwareChunker(max_chars=1_801)


def test_ingestion_checkpoints_batches_and_resumes_without_reembedding() -> None:
    sources, mappings = _manifests(content_suffix="x" * 500)
    store = _CheckpointStore(interrupt_before_claim=2)
    first_embedder = _HashingEmbedder()

    with pytest.raises(RuntimeError, match="before claim"):
        _ingestor(store, first_embedder, batch_size=5).ingest(sources, mappings)

    persisted_after_interruption = len(store.vectors)
    assert persisted_after_interruption == 5
    store.interrupt_before_claim = None
    second_embedder = _HashingEmbedder()
    result = _ingestor(store, second_embedder, batch_size=5).ingest(sources, mappings)

    assert result.reused_vector_count == persisted_after_interruption
    assert sum(len(call) for call in second_embedder.calls) == (
        result.vector_count - persisted_after_interruption
    )


def test_claimed_unpersisted_batch_is_fail_closed_and_published_noop_is_free() -> None:
    sources, mappings = _manifests()
    store = _CheckpointStore()
    failing_embedder = _HashingEmbedder()

    def fail_after_claim(texts: list[str]) -> list[list[float]]:
        failing_embedder.calls.append(tuple(texts))
        raise RuntimeError("synthetic provider success lost before persistence")

    failing_embedder.embed_many = fail_after_claim  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="provider success"):
        _ingestor(store, failing_embedder).ingest(sources, mappings)

    retry_embedder = _HashingEmbedder()
    with pytest.raises(KnowledgeIngestionError, match="outcome_unknown"):
        _ingestor(store, retry_embedder).ingest(sources, mappings)
    assert retry_embedder.calls == []

    complete_store = _CheckpointStore()
    initial_embedder = _HashingEmbedder()
    _ingestor(complete_store, initial_embedder, owner="owner_a").ingest(
        sources, mappings, publish=True
    )
    noop_embedder = _HashingEmbedder()
    result = _ingestor(complete_store, noop_embedder, owner="owner_b").ingest(
        sources, mappings, publish=True
    )
    assert result.published is True
    assert result.reused_vector_count == result.vector_count
    assert noop_embedder.calls == []


def test_policy_changes_corpus_identity_but_not_index_fingerprint() -> None:
    sources, mappings = _manifests()
    first_store = _CheckpointStore()
    second_store = _CheckpointStore()
    embedder = _HashingEmbedder()
    first = _ingestor(first_store, embedder, policy=_policy()).ingest(sources, mappings)
    second = _ingestor(
        second_store,
        _HashingEmbedder(),
        policy=_policy(version="policy_v2", dense_weight=2.0),
    ).ingest(sources, mappings)

    assert first.index_fingerprint == second.index_fingerprint
    assert first.corpus_version_id != second.corpus_version_id


def test_declared_chunker_and_enrichment_versions_bind_effective_behavior() -> None:
    store = _CheckpointStore()
    embedder = _HashingEmbedder()
    with pytest.raises(KnowledgeIngestionError, match="chunker parameters"):
        CorpusIngestor(
            store=store,
            embedder=embedder,
            index_spec=IndexBuildSpec(
                embedding_model=embedder.model,
                embedding_dimension=embedder.dimensions,
                chunker_version="table_chunker_v1",
                enrichment_policy_version="source_keywords_v1",
            ),
            retrieval_policy=_policy(),
            chunker=TableAwareChunker(max_chars=1_000, overlap_chars=100),
        )

    with pytest.raises(KnowledgeIngestionError, match="enrichment policy"):
        CorpusIngestor(
            store=store,
            embedder=embedder,
            index_spec=IndexBuildSpec(
                embedding_model=embedder.model,
                embedding_dimension=embedder.dimensions,
                chunker_version="table_chunker_v1",
                enrichment_policy_version="unimplemented_v2",
            ),
            retrieval_policy=_policy(),
        )


def test_content_only_enrichment_changes_index_input_without_changing_chunks() -> None:
    sources, mappings = _manifests()
    default_store = _CheckpointStore()
    none_store = _CheckpointStore()

    default = _ingestor(default_store, _HashingEmbedder()).ingest(sources, mappings)
    content_only = _ingestor(
        none_store,
        _HashingEmbedder(),
        enrichment_policy_version="none_v1",
    ).ingest(sources, mappings)

    assert default.index_fingerprint != content_only.index_fingerprint
    assert default_store.build is not None
    assert none_store.build is not None
    default_chunk = default_store.build.documents[0].chunks[0]
    content_only_chunk = none_store.build.documents[0].chunks[0]
    assert default_chunk.public_chunk_id == content_only_chunk.public_chunk_id
    assert default_chunk.embedding_input_hash != content_only_chunk.embedding_input_hash
    assert content_only_chunk.search_text == content_only_chunk.content


def test_cli_corpus_version_override_revalidates_both_manifests() -> None:
    cli = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/build_book_corpus.py")
    )
    args = cli["_parser"]().parse_args(
        [
            "--database-url",
            "postgresql+psycopg://fixture.invalid/test",
            "--budget-account-id",
            "account_fixture",
            "--budget-scope-id",
            "scope_fixture",
            "--corpus-version",
            "books-calibrated-v2",
        ]
    )
    sources, mappings = _manifests()
    overridden_sources, overridden_mappings = cli["_override_corpus_version"](
        sources,
        mappings,
        args.corpus_version,
    )

    assert overridden_sources.corpus_version == "books-calibrated-v2"
    assert overridden_mappings.corpus_version == "books-calibrated-v2"
    assert overridden_sources.sources == sources.sources
    assert overridden_mappings.mappings == mappings.mappings
    with pytest.raises(ValidationError):
        cli["_override_corpus_version"](sources, mappings, "")
