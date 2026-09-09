# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
# Modified for thanh-v2 from
# commerce-common/commerce_common/rag/ingestion.py at commit
# 94af718da1858b74b3cb4fba05ddd908ac28d9b4.

"""Deterministic corpus preparation with budget-bound batch embeddings."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from app.knowledge.v2_contracts import (
    CHUNK_OVERLAP_CHARS,
    MAX_CHUNK_CHARS,
    BookMapping,
    BookMappingManifest,
    BookMappingStatus,
    CorpusBuildResult,
    CuratedSource,
    IndexBuildSpec,
    KnowledgeSpan,
    RetrievalPolicy,
    SourceManifest,
    SourceSupportScope,
    canonical_json_sha256,
    content_addressed_id,
    sha256_text,
    sha256_utf8,
    stable_chunk_id,
    stable_span_id,
)
from app.shared.budget import conservative_text_token_bound, current_provider_budget
from app.shared.embedding_runtime import EmbeddingRuntime

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_MAX_SAFE_CHUNK_TOKENS = 6_000
_DEFAULT_EMBEDDING_INPUT_TOKEN_LIMIT = 8_191
_DEFAULT_EMBEDDING_BATCH_TOKEN_LIMIT = 300_000
_CHUNKER_PROFILES = {
    "table_chunker_v1": (MAX_CHUNK_CHARS, CHUNK_OVERLAP_CHARS, 6_000),
    "table_chunker_v2": (1_600, 160, 6_000),
}
_SUPPORTED_ENRICHMENT_POLICIES = frozenset({"none_v1", "source_keywords_v1"})


class KnowledgeIngestionError(ValueError):
    """An input or runtime cannot produce a trustworthy corpus artifact."""


@dataclass(frozen=True, slots=True)
class PreparedChunk:
    chunk_row_id: str
    document_row_id: str
    public_chunk_id: str
    chunk_index: int
    content: str
    search_text: str
    content_hash: str
    embedding_input_hash: str
    token_count: int
    spans: tuple[KnowledgeSpan, ...]


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    document_row_id: str
    source: CuratedSource
    chunks: tuple[PreparedChunk, ...]


@dataclass(frozen=True, slots=True)
class PreparedVector:
    vector_row_id: str
    chunk_row_id: str
    public_chunk_id: str
    embedding_input_hash: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class PreparedBookMapping:
    mapping_row_id: str
    mapping: BookMapping


@dataclass(frozen=True, slots=True)
class PreparedCorpusBuild:
    corpus_version_id: str
    corpus_name: str
    corpus_version: str
    corpus_manifest: Mapping[str, Any]
    index_manifest_id: str
    index_spec: IndexBuildSpec
    index_manifest: Mapping[str, Any]
    retrieval_policy: RetrievalPolicy
    documents: tuple[PreparedDocument, ...]
    mappings: tuple[PreparedBookMapping, ...]
    expected_vector_count: int
    publish: bool


class IngestionStore(Protocol):
    """Short-transaction persistence boundary used around provider work."""

    def prepare_build(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
    ) -> CorpusBuildResult | None: ...

    def find_reusable_vectors(
        self,
        *,
        index_fingerprint: str,
        chunks: Sequence[PreparedChunk],
        embedding_dimension: int,
    ) -> Mapping[str, tuple[float, ...]]: ...

    def claim_embedding_batch(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        chunks: Sequence[PreparedChunk],
    ) -> str | None: ...

    def persist_vector_batch(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        batch_id: str,
        vectors: Sequence[PreparedVector],
    ) -> None: ...

    def persist_reused_vectors(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        vectors: Sequence[PreparedVector],
    ) -> None: ...

    def finalize_build(
        self,
        build: PreparedCorpusBuild,
        *,
        build_owner_id: str,
        reused_source_count: int,
        reused_vector_count: int,
    ) -> CorpusBuildResult: ...


@dataclass(frozen=True, slots=True)
class _ChunkPiece:
    content: str
    table: bool


class TableAwareChunker:
    """Split Markdown tables by row and repeat their header within a hard cap."""

    def __init__(
        self,
        *,
        max_chars: int = MAX_CHUNK_CHARS,
        overlap_chars: int = CHUNK_OVERLAP_CHARS,
        max_tokens: int = _MAX_SAFE_CHUNK_TOKENS,
    ) -> None:
        if not 200 <= max_chars <= MAX_CHUNK_CHARS:
            raise ValueError("chunk size must be between 200 and 1800 characters")
        if not 0 <= overlap_chars < max_chars:
            raise ValueError("chunk overlap must be smaller than chunk size")
        if not 1 <= max_tokens <= _MAX_SAFE_CHUNK_TOKENS:
            raise ValueError("chunk token cap must be between 1 and 6000")
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.max_tokens = max_tokens

    def split(self, text: str) -> tuple[str, ...]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise KnowledgeIngestionError("cannot chunk blank source content")
        pieces: list[_ChunkPiece] = []
        for block in self._blocks(normalized):
            if block.table:
                pieces.extend(self._split_table(block.content))
            else:
                pieces.extend(
                    _ChunkPiece(content=piece, table=False)
                    for piece in self._split_plain(block.content)
                )

        chunks: list[str] = []
        current = ""
        for piece in pieces:
            candidate = f"{current}\n\n{piece.content}" if current else piece.content
            if current and not self._fits(candidate):
                chunks.append(current)
                candidate = piece.content
            if not self._fits(candidate):
                raise KnowledgeIngestionError("chunker produced content above hard cap")
            current = candidate
        if current:
            chunks.append(current)
        if not chunks or any(not chunk or not self._fits(chunk) for chunk in chunks):
            raise KnowledgeIngestionError("chunker produced invalid chunks")
        return tuple(chunks)

    def _fits(self, value: str) -> bool:
        return (
            len(value) <= self.max_chars
            and conservative_text_token_bound(value) <= self.max_tokens
        )

    @staticmethod
    def _blocks(text: str) -> tuple[_ChunkPiece, ...]:
        blocks: list[_ChunkPiece] = []
        current: list[str] = []
        current_is_table: bool | None = None

        def flush() -> None:
            nonlocal current, current_is_table
            if current:
                content = "\n".join(current).strip()
                if content:
                    blocks.append(_ChunkPiece(content, bool(current_is_table)))
            current = []
            current_is_table = None

        for line in text.splitlines():
            if not line.strip():
                flush()
                continue
            is_table = line.count("|") >= 2
            if current_is_table is not None and is_table != current_is_table:
                flush()
            if not is_table and line.lstrip().startswith("#") and current:
                flush()
            current_is_table = is_table
            current.append(line)
        flush()
        return tuple(blocks)

    def _split_table(self, block: str) -> tuple[_ChunkPiece, ...]:
        lines = block.splitlines()
        if len(lines) < 2:
            return tuple(
                _ChunkPiece(content=piece, table=False)
                for piece in self._split_plain(block)
            )
        header_count = 2 if self._is_separator_row(lines[1]) else 1
        header = "\n".join(lines[:header_count])
        rows = lines[header_count:]
        if not self._fits(header):
            raise KnowledgeIngestionError("table header exceeds the hard cap")
        if not rows:
            return (_ChunkPiece(header, True),)

        chunks: list[_ChunkPiece] = []
        current_rows: list[str] = []
        for row in rows:
            candidate = "\n".join((header, *current_rows, row))
            if self._fits(candidate):
                current_rows.append(row)
                continue
            if current_rows:
                chunks.append(_ChunkPiece("\n".join((header, *current_rows)), True))
                current_rows = []
            if self._fits(f"{header}\n{row}"):
                current_rows.append(row)
                continue
            chunks.extend(self._split_table_row(header, row))
        if current_rows:
            chunks.append(_ChunkPiece("\n".join((header, *current_rows)), True))
        return tuple(chunks)

    def _split_table_row(self, header: str, row: str) -> tuple[_ChunkPiece, ...]:
        """Split one oversized row while retaining its column structure and header."""

        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if not cells:
            raise KnowledgeIngestionError("table row has no cells")
        empty_row = self._format_table_cells(["" for _ in cells])
        if not self._fits(f"{header}\n{empty_row}"):
            raise KnowledgeIngestionError("table header leaves no room for a row")

        remaining = list(cells)
        pieces: list[_ChunkPiece] = []
        while any(remaining):
            segment = ["" for _ in remaining]
            progressed = False
            for index, value in enumerate(remaining):
                if not value:
                    continue
                prefix_length = self._largest_table_cell_prefix(
                    header=header,
                    cells=segment,
                    index=index,
                    value=value,
                )
                if prefix_length == 0:
                    continue
                segment[index] = value[:prefix_length]
                remaining[index] = value[prefix_length:]
                progressed = True
            if not progressed:
                raise KnowledgeIngestionError("table row cannot fit beneath its header")
            rendered = f"{header}\n{self._format_table_cells(segment)}"
            if not self._fits(rendered):
                raise KnowledgeIngestionError("table row split exceeded the hard cap")
            pieces.append(_ChunkPiece(rendered, True))
        return tuple(pieces)

    def _largest_table_cell_prefix(
        self,
        *,
        header: str,
        cells: Sequence[str],
        index: int,
        value: str,
    ) -> int:
        low = 0
        high = len(value)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = list(cells)
            candidate[index] = value[:middle]
            rendered = f"{header}\n{self._format_table_cells(candidate)}"
            if self._fits(rendered):
                low = middle
            else:
                high = middle - 1
        return low

    @staticmethod
    def _format_table_cells(cells: Sequence[str]) -> str:
        return f"| {' | '.join(cells)} |"

    @staticmethod
    def _is_separator_row(line: str) -> bool:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return bool(cells) and all(
            _TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in cells
        )

    def _split_plain(self, block: str) -> tuple[str, ...]:
        if self._fits(block):
            return (block,)
        sentences = _SENTENCE_BOUNDARY.split(block)
        if len(sentences) > 1 and all(self._fits(sentence) for sentence in sentences):
            return self._pack_sentences(sentences)
        pieces: list[str] = []
        start = 0
        while start < len(block):
            hard_end = start + self._largest_plain_prefix(block[start:])
            if hard_end <= start:
                raise KnowledgeIngestionError("plain text cannot fit within chunk caps")
            end = hard_end
            if hard_end < len(block):
                whitespace = block.rfind(" ", start + 1, hard_end + 1)
                if whitespace > start:
                    end = whitespace
            piece = block[start:end].strip()
            if not piece:
                piece = block[start:end]
            if not self._fits(piece):
                end = hard_end
                piece = block[start:end]
            if not self._fits(piece):
                raise KnowledgeIngestionError("plain split exceeded the hard cap")
            pieces.append(piece)
            if end >= len(block):
                break
            start = max(end - self.overlap_chars, start + 1)
            while start < len(block) and block[start].isspace():
                start += 1
        return tuple(pieces)

    def _largest_plain_prefix(self, value: str) -> int:
        low = 0
        high = min(len(value), self.max_chars)
        while low < high:
            middle = (low + high + 1) // 2
            if self._fits(value[:middle]):
                low = middle
            else:
                high = middle - 1
        return low

    def _pack_sentences(self, sentences: Sequence[str]) -> tuple[str, ...]:
        pieces: list[str] = []
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}".strip() if current else sentence
            if current and not self._fits(candidate):
                pieces.append(current)
                overlap = current[-self.overlap_chars :].lstrip()
                candidate = f"{overlap} {sentence}".strip() if overlap else sentence
                if not self._fits(candidate):
                    candidate = sentence
            if not self._fits(candidate):
                raise KnowledgeIngestionError("sentence packing exceeded the hard cap")
            current = candidate
        if current:
            pieces.append(current)
        return tuple(pieces)


def embedding_reuse_key(chunk: PreparedChunk) -> str:
    return f"{chunk.public_chunk_id}:{chunk.embedding_input_hash}"


class CorpusIngestor:
    """Prepare, claim, embed, and checkpoint one immutable corpus build."""

    def __init__(
        self,
        *,
        store: IngestionStore,
        embedder: EmbeddingRuntime,
        index_spec: IndexBuildSpec,
        retrieval_policy: RetrievalPolicy,
        chunker: TableAwareChunker | None = None,
        embedding_batch_size: int = 64,
        embedding_input_token_limit: int = _DEFAULT_EMBEDDING_INPUT_TOKEN_LIMIT,
        embedding_batch_token_limit: int = _DEFAULT_EMBEDDING_BATCH_TOKEN_LIMIT,
        build_owner_id: str | None = None,
    ) -> None:
        if not 1 <= embedding_batch_size <= 128:
            raise ValueError("embedding batch size must be between 1 and 128")
        runtime_model = str(getattr(embedder, "model", embedder.method))
        if index_spec.embedding_model != runtime_model:
            raise KnowledgeIngestionError(
                "index embedding model does not match runtime"
            )
        if index_spec.embedding_dimension != embedder.dimensions:
            raise KnowledgeIngestionError(
                "index embedding dimension does not match runtime"
            )
        if embedding_input_token_limit < 1:
            raise ValueError("embedding input token limit must be positive")
        if embedding_batch_token_limit < embedding_input_token_limit:
            raise ValueError("embedding batch token limit must cover one input")
        effective_owner = build_owner_id or f"ingestion_{uuid4().hex}"
        if not effective_owner.strip() or len(effective_owner) > 160:
            raise ValueError("build owner ID must be between 1 and 160 characters")
        expected_chunker = _CHUNKER_PROFILES.get(index_spec.chunker_version)
        if expected_chunker is None:
            raise KnowledgeIngestionError("unsupported chunker version")
        effective_chunker = chunker or TableAwareChunker(
            max_chars=expected_chunker[0],
            overlap_chars=expected_chunker[1],
            max_tokens=expected_chunker[2],
        )
        actual_chunker = (
            effective_chunker.max_chars,
            effective_chunker.overlap_chars,
            effective_chunker.max_tokens,
        )
        if actual_chunker != expected_chunker:
            raise KnowledgeIngestionError(
                "chunker parameters do not match the declared version"
            )
        if index_spec.enrichment_policy_version not in _SUPPORTED_ENRICHMENT_POLICIES:
            raise KnowledgeIngestionError("unsupported enrichment policy version")
        self.store = store
        self.embedder = embedder
        self.index_spec = index_spec
        self.retrieval_policy = retrieval_policy
        self.chunker = effective_chunker
        self.embedding_batch_size = embedding_batch_size
        self.embedding_input_token_limit = embedding_input_token_limit
        self.embedding_batch_token_limit = embedding_batch_token_limit
        self.build_owner_id = effective_owner

    def ingest(
        self,
        sources: SourceManifest,
        mappings: BookMappingManifest,
        *,
        publish: bool = False,
    ) -> CorpusBuildResult:
        validate_manifest_pair(sources, mappings)
        if publish:
            validate_publish_readiness(mappings)
        corpus_version_id = self._corpus_version_id(sources, mappings)
        documents = self._prepare_documents(corpus_version_id, sources.sources)
        all_chunks = tuple(chunk for document in documents for chunk in document.chunks)
        index_manifest_id = content_addressed_id(
            "idx",
            {
                "corpus_version_id": corpus_version_id,
                "index_fingerprint": self.index_spec.index_fingerprint,
            },
        )
        prepared_mappings = tuple(
            PreparedBookMapping(
                mapping_row_id=content_addressed_id(
                    "map",
                    {
                        "corpus_version_id": corpus_version_id,
                        "product_id": mapping.product_id,
                    },
                ),
                mapping=mapping,
            )
            for mapping in sorted(mappings.mappings, key=lambda item: item.product_id)
        )
        build = PreparedCorpusBuild(
            corpus_version_id=corpus_version_id,
            corpus_name=sources.corpus_name,
            corpus_version=sources.corpus_version,
            corpus_manifest=self._corpus_manifest(
                sources, mappings, documents, self.retrieval_policy
            ),
            index_manifest_id=index_manifest_id,
            index_spec=self.index_spec,
            index_manifest={
                "schema_version": "1.0",
                "query_embedding_fingerprint": (
                    self.index_spec.query_embedding_fingerprint
                ),
                "chunk_count": len(all_chunks),
                "vector_count": len(all_chunks),
                "embedding_input_hashes": {
                    chunk.public_chunk_id: chunk.embedding_input_hash
                    for chunk in all_chunks
                },
            },
            retrieval_policy=self.retrieval_policy,
            documents=documents,
            mappings=prepared_mappings,
            expected_vector_count=len(all_chunks),
            publish=publish,
        )
        completed = self.store.prepare_build(
            build,
            build_owner_id=self.build_owner_id,
        )
        if completed is not None:
            return completed
        reusable = self.store.find_reusable_vectors(
            index_fingerprint=self.index_spec.index_fingerprint,
            chunks=all_chunks,
            embedding_dimension=self.index_spec.embedding_dimension,
        )
        self._embed_and_persist_missing(
            build=build,
            chunks=all_chunks,
            reusable=reusable,
        )
        return self.store.finalize_build(
            build,
            build_owner_id=self.build_owner_id,
            reused_source_count=sum(
                bool(document.chunks)
                and all(
                    embedding_reuse_key(chunk) in reusable for chunk in document.chunks
                )
                for document in documents
            ),
            reused_vector_count=len(reusable),
        )

    def _embed_and_persist_missing(
        self,
        *,
        build: PreparedCorpusBuild,
        chunks: Sequence[PreparedChunk],
        reusable: Mapping[str, tuple[float, ...]],
    ) -> None:
        reused_vectors = tuple(
            self._prepared_vector(build, chunk, reusable[embedding_reuse_key(chunk)])
            for chunk in chunks
            if embedding_reuse_key(chunk) in reusable
        )
        for start in range(0, len(reused_vectors), self.embedding_batch_size):
            self.store.persist_reused_vectors(
                build,
                build_owner_id=self.build_owner_id,
                vectors=reused_vectors[start : start + self.embedding_batch_size],
            )
        missing = [
            chunk for chunk in chunks if embedding_reuse_key(chunk) not in reusable
        ]
        if missing and self.embedder.method.startswith("openai_"):
            if current_provider_budget() is None:
                raise KnowledgeIngestionError(
                    "OpenAI ingestion requires a shared provider budget context"
                )
        for batch in self._embedding_batches(missing):
            batch_id = self.store.claim_embedding_batch(
                build,
                build_owner_id=self.build_owner_id,
                chunks=batch,
            )
            if batch_id is None:
                continue
            result = self.embedder.embed_many([chunk.search_text for chunk in batch])
            if len(result) != len(batch):
                raise KnowledgeIngestionError(
                    "embedding runtime returned the wrong count"
                )
            vectors = tuple(
                self._prepared_vector(build, chunk, vector)
                for chunk, vector in zip(batch, result, strict=True)
            )
            self.store.persist_vector_batch(
                build,
                build_owner_id=self.build_owner_id,
                batch_id=batch_id,
                vectors=vectors,
            )

    def _prepared_vector(
        self,
        build: PreparedCorpusBuild,
        chunk: PreparedChunk,
        vector: Sequence[float],
    ) -> PreparedVector:
        return PreparedVector(
            vector_row_id=content_addressed_id(
                "vec",
                {
                    "corpus_version_id": build.corpus_version_id,
                    "public_chunk_id": chunk.public_chunk_id,
                    "index_fingerprint": self.index_spec.index_fingerprint,
                    "embedding_input_hash": chunk.embedding_input_hash,
                },
            ),
            chunk_row_id=chunk.chunk_row_id,
            public_chunk_id=chunk.public_chunk_id,
            embedding_input_hash=chunk.embedding_input_hash,
            vector=self._validate_vector(vector),
        )

    def _embedding_batches(
        self, chunks: Sequence[PreparedChunk]
    ) -> tuple[tuple[PreparedChunk, ...], ...]:
        batches: list[tuple[PreparedChunk, ...]] = []
        current: list[PreparedChunk] = []
        current_bound = 0
        for chunk in chunks:
            bound = conservative_text_token_bound(chunk.search_text)
            if bound > self.embedding_input_token_limit:
                raise KnowledgeIngestionError(
                    "embedding input exceeds the conservative token limit"
                )
            if current and (
                len(current) >= self.embedding_batch_size
                or current_bound + bound > self.embedding_batch_token_limit
            ):
                batches.append(tuple(current))
                current = []
                current_bound = 0
            current.append(chunk)
            current_bound += bound
        if current:
            batches.append(tuple(current))
        return tuple(batches)

    def _validate_vector(self, vector: Sequence[float]) -> tuple[float, ...]:
        if len(vector) != self.index_spec.embedding_dimension:
            raise KnowledgeIngestionError("embedding vector dimension is incompatible")
        checked = tuple(float(component) for component in vector)
        if any(not math.isfinite(component) for component in checked):
            raise KnowledgeIngestionError("embedding vector components must be finite")
        return checked

    def _prepare_documents(
        self,
        corpus_version_id: str,
        sources: Sequence[CuratedSource],
    ) -> tuple[PreparedDocument, ...]:
        documents: list[PreparedDocument] = []
        for source in sorted(sources, key=lambda item: item.source_id):
            document_row_id = content_addressed_id(
                "doc",
                {
                    "corpus_version_id": corpus_version_id,
                    "source_version_id": source.source_version_id,
                },
            )
            chunks: list[PreparedChunk] = []
            for chunk_index, content in enumerate(
                self.chunker.split(source.content_markdown)
            ):
                content_hash = sha256_text(content)
                public_chunk_id = stable_chunk_id(
                    source_version_id=source.source_version_id,
                    chunker_version=self.index_spec.chunker_version,
                    chunk_index=chunk_index,
                    content_hash=content_hash,
                )
                span_hash = sha256_utf8(content)
                span = KnowledgeSpan(
                    span_id=stable_span_id(
                        chunk_id=public_chunk_id,
                        start_char=0,
                        end_char=len(content),
                        content_hash=span_hash,
                    ),
                    start_char=0,
                    end_char=len(content),
                    content_hash=span_hash,
                )
                search_text = self._search_text(source, content)
                if (
                    conservative_text_token_bound(search_text)
                    > self.embedding_input_token_limit
                ):
                    raise KnowledgeIngestionError(
                        "embedding input exceeds the conservative token limit"
                    )
                chunks.append(
                    PreparedChunk(
                        chunk_row_id=content_addressed_id(
                            "row",
                            {
                                "corpus_version_id": corpus_version_id,
                                "public_chunk_id": public_chunk_id,
                            },
                        ),
                        document_row_id=document_row_id,
                        public_chunk_id=public_chunk_id,
                        chunk_index=chunk_index,
                        content=content,
                        search_text=search_text,
                        content_hash=content_hash,
                        embedding_input_hash=sha256_text(search_text),
                        token_count=conservative_text_token_bound(content),
                        spans=(span,),
                    )
                )
            documents.append(
                PreparedDocument(
                    document_row_id=document_row_id,
                    source=source,
                    chunks=tuple(chunks),
                )
            )
        return tuple(documents)

    def _search_text(self, source: CuratedSource, content: str) -> str:
        if self.index_spec.enrichment_policy_version == "none_v1":
            return content
        return "\n".join(
            part
            for part in (
                source.title,
                " ".join(source.keywords),
                content,
            )
            if part
        )

    @staticmethod
    def _validate_mapping_source(mapping: BookMapping, source: CuratedSource) -> None:
        if mapping.mapping_status == BookMappingStatus.EXACT_WORK:
            if (
                source.support_scope != SourceSupportScope.WORK
                or source.work_identifier != mapping.work_identifier
            ):
                raise KnowledgeIngestionError(
                    "exact work mapping can use only matching work sources"
                )
            return
        if mapping.mapping_status != BookMappingStatus.EXACT_EDITION:
            raise KnowledgeIngestionError("unresolved mapping cannot publish a source")
        if source.support_scope == SourceSupportScope.EDITION:
            if source.edition_identifier != mapping.edition_identifier:
                raise KnowledgeIngestionError(
                    "edition source does not match mapped edition"
                )
            return
        if (
            mapping.work_identifier is None
            or source.work_identifier != mapping.work_identifier
        ):
            raise KnowledgeIngestionError(
                "work source does not match mapped edition work"
            )

    def _corpus_version_id(
        self, sources: SourceManifest, mappings: BookMappingManifest
    ) -> str:
        return content_addressed_id(
            "cor",
            {
                "corpus_name": sources.corpus_name,
                "corpus_version": sources.corpus_version,
                "catalog_source_commit": sources.catalog_source_commit,
                "source_manifest_sha256": canonical_json_sha256(
                    sources.model_dump(mode="json")
                ),
                "mapping_manifest_sha256": canonical_json_sha256(
                    mappings.model_dump(mode="json")
                ),
                "retrieval_policy": self.retrieval_policy.model_dump(mode="json"),
            },
        )

    @staticmethod
    def _corpus_manifest(
        sources: SourceManifest,
        mappings: BookMappingManifest,
        documents: Sequence[PreparedDocument],
        retrieval_policy: RetrievalPolicy,
    ) -> Mapping[str, Any]:
        return {
            "schema_version": "1.0",
            "catalog_source_commit": sources.catalog_source_commit,
            "source_manifest_sha256": canonical_json_sha256(
                sources.model_dump(mode="json")
            ),
            "mapping_manifest_sha256": canonical_json_sha256(
                mappings.model_dump(mode="json")
            ),
            "source_version_ids": sorted(
                document.source.source_version_id for document in documents
            ),
            "source_count": len(documents),
            "mapping_count": len(mappings.mappings),
            "retrieval_policy": retrieval_policy.model_dump(mode="json"),
        }


def validate_manifest_pair(
    sources: SourceManifest, mappings: BookMappingManifest
) -> None:
    """Validate cross-file identity, source linkage, and claim support scope."""

    identity = (
        sources.corpus_name,
        sources.corpus_version,
        sources.catalog_source_commit,
    )
    if identity != (
        mappings.corpus_name,
        mappings.corpus_version,
        mappings.catalog_source_commit,
    ):
        raise KnowledgeIngestionError("source and mapping manifests do not match")
    source_by_id = {source.source_id: source for source in sources.sources}
    referenced_sources: set[str] = set()
    for mapping in mappings.mappings:
        for source_id in mapping.source_ids:
            source = source_by_id.get(source_id)
            if source is None:
                raise KnowledgeIngestionError("mapping references an unknown source")
            referenced_sources.add(source_id)
            CorpusIngestor._validate_mapping_source(mapping, source)
        if mapping.mapping_status == BookMappingStatus.EXACT_EDITION:
            if not any(
                source_by_id[source_id].support_scope == SourceSupportScope.EDITION
                for source_id in mapping.source_ids
            ):
                raise KnowledgeIngestionError(
                    "exact edition mapping requires edition-scoped evidence"
                )
    if referenced_sources != set(source_by_id):
        raise KnowledgeIngestionError(
            "every curated source must support an exact published mapping"
        )


def validate_publish_readiness(mappings: BookMappingManifest) -> None:
    """Reject a publication plan that cannot satisfy the Package 3 coverage gate."""

    if len(mappings.mappings) != mappings.catalog_book_count:
        raise KnowledgeIngestionError(
            "publication requires one mapping for every catalog book"
        )
    supported_work_ids = {
        mapping.work_identifier
        for mapping in mappings.mappings
        if mapping.mapping_status
        in {BookMappingStatus.EXACT_WORK, BookMappingStatus.EXACT_EDITION}
        and mapping.work_identifier is not None
    }
    if len(supported_work_ids) < 20:
        raise KnowledgeIngestionError(
            "publication requires sources for at least 20 distinct works"
        )
