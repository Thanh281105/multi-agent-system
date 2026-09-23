# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
# Modified for thanh-v2 from commit
# 94af718da1858b74b3cb4fba05ddd908ac28d9b4.

"""Strict contracts for reproducible, versioned v2 knowledge artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.v2.contracts import ConversationMode, ProductId, V2Contract

SOURCE_MANIFEST_SCHEMA_VERSION = "1.0"
MAX_SOURCE_CONTENT_CHARS = 100_000
MAX_CHUNK_CHARS = 1_800
CHUNK_OVERLAP_CHARS = 180

SHA256_PATTERN = r"^[0-9a-f]{64}$"
SOURCE_ID_PATTERN = r"^src_[a-z0-9][a-z0-9_-]{0,58}$"
CONTENT_ID_PATTERN = r"^(cor|idx|doc|row|vec|map|svr|chk|spn|evd)_[0-9a-f]{60}$"
WORK_ID_PATTERN = r"^work_[a-z0-9]+(?:_[a-z0-9]+)*$"
EDITION_ID_PATTERN = r"^edition_[a-z0-9]+(?:_[a-z0-9]+)*$"
VERSION_PATTERN = r"^\d{4}-\d{2}-\d{2}(?:\.[1-9][0-9]*)?$"
SCOPE_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"
LANGUAGE_PATTERN = r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"
TENANT_ID_PATTERN = r"^[a-z][a-z0-9_-]{2,127}$"

Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
SourceId = Annotated[str, Field(pattern=SOURCE_ID_PATTERN)]
ContentAddressedId = Annotated[str, Field(pattern=CONTENT_ID_PATTERN)]
WorkIdentifier = Annotated[str, Field(pattern=WORK_ID_PATTERN, max_length=200)]
EditionIdentifier = Annotated[str, Field(pattern=EDITION_ID_PATTERN, max_length=200)]
Scope = Annotated[str, Field(pattern=SCOPE_PATTERN)]
TenantIdentifier = Annotated[str, Field(pattern=TENANT_ID_PATTERN)]
PrincipalIdentifier = Annotated[str, Field(min_length=1, max_length=160)]

_TRAILING_WHITESPACE = re.compile(r"[\t ]+$")
_EXCESS_BLANK_LINES = re.compile(r"\n{4,}")


def normalize_source_content(value: str) -> str:
    """Return the canonical UTF-8 text representation used for source hashes."""

    normalized = unicodedata.normalize("NFC", value.removeprefix("\ufeff"))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_TRAILING_WHITESPACE.sub("", line) for line in normalized.split("\n")]
    normalized = "\n".join(lines).strip("\n")
    normalized = _EXCESS_BLANK_LINES.sub("\n\n\n", normalized)
    if not normalized.strip():
        raise ValueError("source content must not be blank")
    return f"{normalized}\n"


def sha256_text(value: str) -> str:
    """Hash canonical source text with SHA-256."""

    return hashlib.sha256(normalize_source_content(value).encode("utf-8")).hexdigest()


def sha256_utf8(value: str) -> str:
    """Hash exact UTF-8 text without applying source-document normalization."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    """Hash a JSON-compatible value with a stable UTF-8 framing."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def content_addressed_id(prefix: str, value: Any) -> str:
    """Build a database-sized identifier from a canonical JSON payload."""

    if prefix not in {
        "cor",
        "idx",
        "doc",
        "row",
        "vec",
        "map",
        "svr",
        "chk",
        "spn",
        "evd",
    }:
        raise ValueError("unsupported content-addressed ID prefix")
    return f"{prefix}_{canonical_json_sha256(value)[:60]}"


def stable_chunk_id(
    *,
    source_version_id: str,
    chunker_version: str,
    chunk_index: int,
    content_hash: str,
) -> str:
    """Identify a public chunk independently of its corpus-scoped database row."""

    return content_addressed_id(
        "chk",
        {
            "source_version_id": source_version_id,
            "chunker_version": chunker_version,
            "chunk_index": chunk_index,
            "content_hash": content_hash,
        },
    )


def stable_span_id(
    *, chunk_id: str, start_char: int, end_char: int, content_hash: str
) -> str:
    """Identify one exact chunk-local excerpt."""

    return content_addressed_id(
        "spn",
        {
            "chunk_id": chunk_id,
            "start_char": start_char,
            "end_char": end_char,
            "content_hash": content_hash,
        },
    )


def stable_evidence_id(
    *, source_id: str, source_version_id: str, chunk_id: str, span_id: str
) -> str:
    """Identify citation evidence without using a response-local display label."""

    return content_addressed_id(
        "evd",
        {
            "source_id": source_id,
            "source_version_id": source_version_id,
            "chunk_id": chunk_id,
            "span_id": span_id,
        },
    )


class SourceKind(StrEnum):
    AUTHOR_SITE = "author_site"
    PUBLISHER_SITE = "publisher_site"
    LIBRARY_CATALOG = "library_catalog"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    ENCYCLOPEDIA = "encyclopedia"
    OPEN_LICENSED_TEXT = "open_licensed_text"
    DEMO_POLICY = "demo_policy"


class SourceSupportScope(StrEnum):
    WORK = "work"
    EDITION = "edition"


class PrincipalAccessPolicy(StrEnum):
    AUTHENTICATED = "authenticated"
    ALLOWLIST = "allowlist"


class BookMappingStatus(StrEnum):
    EXACT_EDITION = "exact_edition"
    EXACT_WORK = "exact_work"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


class SourceAccessMetadata(V2Contract):
    """Server-owned ACL fields that map directly to PostgreSQL predicates."""

    allowed_tenant_ids: tuple[TenantIdentifier, ...] = Field(
        min_length=1, max_length=64
    )
    principal_policy: PrincipalAccessPolicy
    allowed_principal_ids: tuple[PrincipalIdentifier, ...] = Field(
        default=(), max_length=256
    )
    shopper_required_scopes: tuple[Scope, ...] | None
    merchant_required_scopes: tuple[Scope, ...] | None

    @field_validator(
        "allowed_tenant_ids",
        "allowed_principal_ids",
        "shopper_required_scopes",
        "merchant_required_scopes",
    )
    @classmethod
    def sort_unique_values(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("access metadata values must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("access metadata values must be unique")
        return tuple(sorted(cleaned))

    @model_validator(mode="after")
    def validate_policy(self) -> SourceAccessMetadata:
        if self.principal_policy == PrincipalAccessPolicy.AUTHENTICATED:
            if self.allowed_principal_ids:
                raise ValueError(
                    "authenticated access cannot include a principal allowlist"
                )
        elif not self.allowed_principal_ids:
            raise ValueError("allowlist access requires at least one principal")
        if (
            self.shopper_required_scopes is None
            and self.merchant_required_scopes is None
        ):
            raise ValueError("source access must grant at least one conversation mode")
        if self.shopper_required_scopes is not None and "ecommerce.read" not in (
            self.shopper_required_scopes
        ):
            raise ValueError("shopper source access requires ecommerce.read")
        merchant_base = {"ecommerce.read", "merchant.read"}
        if self.merchant_required_scopes is not None and not merchant_base <= set(
            self.merchant_required_scopes
        ):
            raise ValueError(
                "merchant source access requires merchant.read and ecommerce.read"
            )
        return self

    def required_scopes(self, mode: ConversationMode) -> frozenset[str] | None:
        value = (
            self.shopper_required_scopes
            if mode == ConversationMode.SHOPPER
            else self.merchant_required_scopes
        )
        return None if value is None else frozenset(value)


class SourceMetadata(V2Contract):
    """Bounded bibliographic and rights metadata for one approved source."""

    authors: tuple[str, ...] = Field(default=(), max_length=32)
    publisher: str | None = Field(default=None, min_length=1, max_length=300)
    publication_year: int | None = Field(default=None, strict=True, ge=1_000, le=3_000)
    isbn: tuple[str, ...] = Field(default=(), max_length=16)
    license_name: str | None = Field(default=None, min_length=1, max_length=200)
    license_url: HttpUrl | None = None

    @field_validator("authors", "isbn")
    @classmethod
    def validate_unique_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned) or len(cleaned) != len(set(cleaned)):
            raise ValueError("source metadata lists require unique non-blank values")
        return cleaned


class CuratedSource(V2Contract):
    """One approved, bounded public source revision."""

    source_id: SourceId
    title: str = Field(min_length=1, max_length=500)
    source_url: HttpUrl
    source_kind: SourceKind
    use_basis: str = Field(min_length=1, max_length=500)
    retrieved_at: AwareDatetime
    document_version: str = Field(pattern=VERSION_PATTERN)
    language: str = Field(pattern=LANGUAGE_PATTERN, max_length=35)
    content_markdown: str = Field(min_length=1, max_length=MAX_SOURCE_CONTENT_CHARS)
    content_sha256: Sha256
    support_scope: SourceSupportScope
    work_identifier: WorkIdentifier | None = None
    edition_identifier: EditionIdentifier | None = None
    access: SourceAccessMetadata
    keywords: tuple[str, ...] = Field(default=(), max_length=32)
    metadata: SourceMetadata = Field(default_factory=SourceMetadata)

    @field_validator("title", "use_basis")
    @classmethod
    def strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("source text fields must not be blank")
        return cleaned

    @field_validator("content_markdown", mode="before")
    @classmethod
    def normalize_content(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("source content must be text")
        return normalize_source_content(value)

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(keyword.strip() for keyword in value)
        if any(not keyword or len(keyword) > 120 for keyword in cleaned):
            raise ValueError("source keywords must be non-blank and at most 120 chars")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("source keywords must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_source(self) -> CuratedSource:
        if sha256_text(self.content_markdown) != self.content_sha256:
            raise ValueError("source content hash does not match canonical content")
        if self.support_scope == SourceSupportScope.WORK:
            if self.work_identifier is None or self.edition_identifier is not None:
                raise ValueError("work source requires only a work identifier")
        elif self.edition_identifier is None:
            raise ValueError("edition source requires an edition identifier")
        return self

    @property
    def source_version_id(self) -> str:
        return content_addressed_id(
            "svr",
            {
                "source_id": self.source_id,
                "document_version": self.document_version,
                "content_sha256": self.content_sha256,
            },
        )


class SourceManifest(V2Contract):
    schema_version: Literal["1.0"] = "1.0"
    corpus_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,119}$")
    corpus_version: str = Field(min_length=1, max_length=120)
    catalog_source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    sources: tuple[CuratedSource, ...] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_sources(self) -> SourceManifest:
        source_ids = [source.source_id for source in self.sources]
        source_versions = [source.source_version_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source IDs must be unique within a manifest")
        if len(source_versions) != len(set(source_versions)):
            raise ValueError("source versions must be unique within a manifest")
        return self


class BookMapping(V2Contract):
    product_id: ProductId
    mapping_status: BookMappingStatus
    source_ids: tuple[SourceId, ...] = Field(default=(), max_length=32)
    work_identifier: WorkIdentifier | None = None
    edition_identifier: EditionIdentifier | None = None
    mapping_evidence: str = Field(min_length=1, max_length=2_000)
    ambiguity_reason: str | None = Field(default=None, min_length=1, max_length=1_000)
    unmatched_reason: str | None = Field(default=None, min_length=1, max_length=1_000)

    @field_validator("source_ids")
    @classmethod
    def sort_unique_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("mapping source IDs must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_mapping(self) -> BookMapping:
        if self.mapping_status == BookMappingStatus.EXACT_EDITION:
            if not self.source_ids or self.edition_identifier is None:
                raise ValueError(
                    "exact edition mapping requires sources and edition ID"
                )
            if self.ambiguity_reason is not None or self.unmatched_reason is not None:
                raise ValueError("exact mapping cannot include unresolved reasons")
        elif self.mapping_status == BookMappingStatus.EXACT_WORK:
            if not self.source_ids or self.work_identifier is None:
                raise ValueError("exact work mapping requires sources and work ID")
            if self.edition_identifier is not None:
                raise ValueError("exact work mapping cannot claim an edition")
            if self.ambiguity_reason is not None or self.unmatched_reason is not None:
                raise ValueError("exact mapping cannot include unresolved reasons")
        elif self.mapping_status == BookMappingStatus.AMBIGUOUS:
            if self.source_ids or self.work_identifier or self.edition_identifier:
                raise ValueError("ambiguous mapping cannot publish source relations")
            if self.ambiguity_reason is None or self.unmatched_reason is not None:
                raise ValueError("ambiguous mapping requires only ambiguity_reason")
        elif (
            self.source_ids
            or self.work_identifier
            or self.edition_identifier
            or self.unmatched_reason is None
            or self.ambiguity_reason is not None
        ):
            raise ValueError("unmatched mapping requires only unmatched_reason")
        return self


class BookMappingManifest(V2Contract):
    schema_version: Literal["1.0"] = "1.0"
    corpus_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,119}$")
    corpus_version: str = Field(min_length=1, max_length=120)
    catalog_source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    catalog_book_count: Literal[200]
    mappings: tuple[BookMapping, ...] = Field(min_length=200, max_length=200)

    @model_validator(mode="after")
    def validate_mappings(self) -> BookMappingManifest:
        product_ids = [mapping.product_id for mapping in self.mappings]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("mapping manifest must contain unique product IDs")
        return self


class RetrievalPolicy(V2Contract):
    """Frozen ranking policy; excluded from embedding/index fingerprints."""

    version: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,119}$")
    dense_weight: float = Field(strict=True, ge=0, le=10)
    lexical_weight: float = Field(strict=True, ge=0, le=10)
    rrf_k: int = Field(default=60, strict=True, ge=1, le=1_000)
    candidate_limit: int = Field(default=30, strict=True, ge=1, le=100)
    default_limit: Literal[6] = 6
    max_limit: Literal[8] = 8
    max_context_tokens: Literal[6000] = 6_000
    min_dense_relevance: float = Field(strict=True, ge=-1, le=1)
    min_lexical_coverage: float = Field(strict=True, ge=0, le=1)

    @model_validator(mode="after")
    def validate_weights(self) -> RetrievalPolicy:
        if self.dense_weight == 0 and self.lexical_weight == 0:
            raise ValueError("at least one retrieval channel must have positive weight")
        if self.candidate_limit < self.max_limit:
            raise ValueError("candidate limit cannot be lower than result limit")
        return self


class IndexBuildSpec(V2Contract):
    embedding_model: str = Field(min_length=1, max_length=160)
    embedding_dimension: int = Field(strict=True, ge=32, le=4_096)
    chunker_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,119}$")
    enrichment_policy_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,119}$")

    @property
    def index_fingerprint(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))

    @property
    def query_embedding_fingerprint(self) -> str:
        return canonical_json_sha256(
            {
                "embedding_model": self.embedding_model,
                "embedding_dimension": self.embedding_dimension,
            }
        )


class PublishedKnowledgeSnapshot(V2Contract):
    corpus_version_id: ContentAddressedId
    corpus_name: str = Field(min_length=1, max_length=120)
    corpus_version: str = Field(min_length=1, max_length=120)
    index_manifest_id: ContentAddressedId
    index_fingerprint: Sha256
    embedding_model: str = Field(min_length=1, max_length=160)
    embedding_dimension: int = Field(strict=True, ge=32, le=4_096)
    chunker_version: str = Field(min_length=1, max_length=120)
    enrichment_policy_version: str = Field(min_length=1, max_length=120)
    query_embedding_fingerprint: Sha256
    retrieval_policy: RetrievalPolicy
    published_at: AwareDatetime


class AuthorizedKnowledgeSource(V2Contract):
    source_id: SourceId
    source_version_id: ContentAddressedId
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    planning_text: str = Field(min_length=1, max_length=2_000)
    keywords: tuple[str, ...] = Field(default=(), max_length=32)
    support_scope: SourceSupportScope
    work_identifier: WorkIdentifier | None = None
    edition_identifier: EditionIdentifier | None = None
    retrieved_at: AwareDatetime


class KnowledgeSpan(V2Contract):
    span_id: ContentAddressedId
    start_char: int = Field(strict=True, ge=0)
    end_char: int = Field(strict=True, gt=0)
    content_hash: Sha256

    @model_validator(mode="after")
    def validate_bounds(self) -> KnowledgeSpan:
        if self.end_char <= self.start_char:
            raise ValueError("span end must follow its start")
        return self


class RetrievalChunk(V2Contract):
    corpus_version_id: ContentAddressedId
    index_manifest_id: ContentAddressedId
    source_id: SourceId
    source_version_id: ContentAddressedId
    chunk_id: ContentAddressedId
    chunk_index: int = Field(strict=True, ge=0)
    chunker_version: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,119}$")
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    content: str = Field(min_length=1, max_length=MAX_CHUNK_CHARS)
    token_count: int = Field(strict=True, ge=1, le=6_000)
    content_hash: Sha256
    vector: tuple[float, ...] = Field(min_length=32, max_length=4_096)
    spans: tuple[KnowledgeSpan, ...] = Field(min_length=1, max_length=256)
    support_scope: SourceSupportScope
    work_identifier: WorkIdentifier | None = None
    edition_identifier: EditionIdentifier | None = None

    @model_validator(mode="after")
    def validate_spans(self) -> RetrievalChunk:
        if sha256_text(self.content) != self.content_hash:
            raise ValueError("chunk content hash does not match content")
        expected_chunk_id = stable_chunk_id(
            source_version_id=self.source_version_id,
            chunker_version=self.chunker_version,
            chunk_index=self.chunk_index,
            content_hash=self.content_hash,
        )
        if self.chunk_id != expected_chunk_id:
            raise ValueError("chunk ID does not match stable public identity")
        for span in self.spans:
            if span.end_char > len(self.content):
                raise ValueError("chunk span exceeds content bounds")
            excerpt_hash = sha256_utf8(self.content[span.start_char : span.end_char])
            if span.content_hash != excerpt_hash:
                raise ValueError("span content hash does not match chunk excerpt")
            expected_span_id = stable_span_id(
                chunk_id=self.chunk_id,
                start_char=span.start_char,
                end_char=span.end_char,
                content_hash=span.content_hash,
            )
            if span.span_id != expected_span_id:
                raise ValueError("span ID does not match stable public identity")
        span_ids = [span.span_id for span in self.spans]
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("chunk span IDs must be unique")
        if any(not math.isfinite(component) for component in self.vector):
            raise ValueError("chunk vector components must be finite")
        return self


class ResolvedKnowledgeEvidence(V2Contract):
    evidence_id: ContentAddressedId
    corpus_version_id: ContentAddressedId
    source_id: SourceId
    source_version_id: ContentAddressedId
    chunk_id: ContentAddressedId
    span_id: ContentAddressedId
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    excerpt: str = Field(min_length=1, max_length=MAX_CHUNK_CHARS)
    content_hash: Sha256
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_evidence(self) -> ResolvedKnowledgeEvidence:
        if sha256_utf8(self.excerpt) != self.content_hash:
            raise ValueError("evidence content hash does not match excerpt")
        expected_id = stable_evidence_id(
            source_id=self.source_id,
            source_version_id=self.source_version_id,
            chunk_id=self.chunk_id,
            span_id=self.span_id,
        )
        if self.evidence_id != expected_id:
            raise ValueError("evidence ID does not match stable public identity")
        return self


class CorpusBuildResult(V2Contract):
    corpus_version_id: ContentAddressedId
    index_manifest_id: ContentAddressedId
    index_fingerprint: Sha256
    source_count: int = Field(strict=True, ge=0)
    chunk_count: int = Field(strict=True, ge=0)
    vector_count: int = Field(strict=True, ge=0)
    mapping_count: int = Field(strict=True, ge=0)
    reused_source_count: int = Field(strict=True, ge=0)
    reused_vector_count: int = Field(strict=True, ge=0)
    published: bool
