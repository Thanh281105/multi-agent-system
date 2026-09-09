"""Deterministic and negative coverage for v2 knowledge ingestion contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.knowledge.v2_contracts import (
    CuratedSource,
    KnowledgeSpan,
    PrincipalAccessPolicy,
    RetrievalChunk,
    SourceAccessMetadata,
    SourceKind,
    SourceSupportScope,
    content_addressed_id,
    normalize_source_content,
    sha256_text,
    sha256_utf8,
    stable_chunk_id,
    stable_span_id,
)


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
