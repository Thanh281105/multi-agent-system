"""Synthetic development regressions, never held-out benchmark measurements."""

from __future__ import annotations

import json
import unicodedata
from types import SimpleNamespace

import pytest

from app.knowledge.retrieval import (
    HybridKnowledgeRetriever,
    ModelRuntimeKnowledgeQueryPlanner,
)
from app.shared.budget import GENERATION_INPUT_TOKEN_LIMIT, provider_budget_scope
from app.v2.answers import (
    EvidenceRequirement,
    _bounded_model_answer_schema,
    _bounded_model_input,
    _model_payload_token_bound,
)
from app.v2.contracts import EvidenceKind
from app.v2.planning import BoundedV2Planner
from app.v2.runtime_contracts import ToolEvidence
from app.v2.supervisor import _grounding_requirements
from tests.test_knowledge_retrieval import (
    FakePlanningRuntime,
    FakeStore,
    StaticEmbedder,
    _access,
    _budget_context,
    _chunk,
    _policy,
    _snapshot,
    _source,
    _vector,
)
from tests.test_v2_answers import _knowledge_evidence
from tests.test_v2_supervisor import _context


@pytest.mark.asyncio
async def test_model_rewrite_retains_original_identifier() -> None:
    planner = ModelRuntimeKnowledgeQueryPlanner(
        FakePlanningRuntime((_source(1).source_id,))
    )
    with provider_budget_scope(_budget_context()):
        plan = await planner.plan("Orion ZX-314", (_source(1),))
    assert plan.queries[0] == "Orion ZX-314"
    assert "rewritten query" in plan.queries
    assert len(plan.queries) <= 4


@pytest.mark.asyncio
async def test_title_context_recovers_a_lexical_hit_without_fabricating_evidence() -> (
    None
):
    source = _source(1, title="Orion Fieldbook")
    snapshot = _snapshot(policy=_policy(dense_weight=0.0))
    chunk = _chunk(
        snapshot, source, content="It describes remote estuaries.", vector=_vector(1)
    )
    store = FakeStore(snapshot, (source,), (chunk,))
    bundle = await HybridKnowledgeRetriever(store, StaticEmbedder()).retrieve(
        "Orion Fieldbook", _access(), corpus_version_id=snapshot.corpus_version_id
    )
    assert len(bundle.hits) == 1
    assert bundle.hits[0].chunk.content == chunk.content
    assert bundle.hits[0].span.content_hash == chunk.spans[0].content_hash


@pytest.mark.asyncio
async def test_equal_text_in_distinct_sources_keeps_both_attributions() -> None:
    sources = (_source(1), _source(2))
    snapshot = _snapshot()
    chunks = tuple(
        _chunk(
            snapshot,
            s,
            content="A shared public bibliographic note.",
            vector=_vector(0),
        )
        for s in sources
    )
    bundle = await HybridKnowledgeRetriever(
        FakeStore(snapshot, sources, chunks), StaticEmbedder()
    ).retrieve(
        "bibliographic note", _access(), corpus_version_id=snapshot.corpus_version_id
    )
    assert {h.source.source_id for h in bundle.hits} == {s.source_id for s in sources}


def test_long_excerpt_keeps_late_detail_within_input_budget() -> None:
    text = "Public catalog context. " * 20 + "The translator is Mira Sen."
    evidence, _ = _knowledge_evidence(text=text)
    # Exercise the actual draft payload builder without dispatching a provider.

    payload = _bounded_model_input(
        evidence,
        (EvidenceRequirement(EvidenceKind.KNOWLEDGE, "product_1"),),
        instructions="Use only the supplied evidence.",
        schema=_bounded_model_answer_schema(evidence),
        envelope=lambda catalog: {
            "request": "Who translated this work?",
            "catalog": catalog,
        },
    )
    excerpt = json.loads(payload)["catalog"]["extracts"][0]["x"]
    assert excerpt == text
    assert "Mira Sen" in excerpt


@pytest.mark.asyncio
async def test_constraints_require_answer_fields_for_each_candidate() -> None:
    planned = await BoundedV2Planner().plan(
        "Tìm sách Orion giá dưới 100000 đồng, rating ít nhất 4.5 và tối đa 300 trang.",
        _context(),
    )
    assessment = SimpleNamespace(
        fulfilled_obligation_ids=tuple(o.obligation_id for o in planned.obligations),
        candidate_product_ids=(1, 2),
    )
    requirements = _grounding_requirements(planned, assessment)
    for product_id in (1, 2):
        fields = {
            r.field
            for r in requirements
            if r.kind == EvidenceKind.CATALOG
            and r.subject_id == f"product_{product_id}"
        }
        assert {
            "title",
            "snapshot_price_vnd",
            "snapshot_rating",
            "page_count",
        } <= fields


@pytest.mark.asyncio
async def test_canonically_equivalent_vietnamese_query_retrieves_same_source() -> None:
    source = _source(1)
    snapshot = _snapshot(policy=_policy(dense_weight=0.0))
    chunk = _chunk(snapshot, source, content="cộng hưởng", vector=_vector(1))
    retriever = HybridKnowledgeRetriever(
        FakeStore(snapshot, (source,), (chunk,)), StaticEmbedder()
    )
    for query in ("cộng hưởng", unicodedata.normalize("NFD", "cộng hưởng")):
        bundle = await retriever.retrieve(
            query, _access(), corpus_version_id=snapshot.corpus_version_id
        )
        assert len(bundle.hits) == 1
        assert bundle.hits[0].chunk.chunk_id == chunk.chunk_id


@pytest.mark.asyncio
async def test_duplicate_text_within_one_source_version_is_still_deduplicated() -> None:
    source = _source(1)
    snapshot = _snapshot()
    chunks = tuple(
        _chunk(
            snapshot,
            source,
            content="One shared note.",
            vector=_vector(0),
            chunk_index=index,
        )
        for index in (0, 1)
    )
    bundle = await HybridKnowledgeRetriever(
        FakeStore(snapshot, (source,), chunks), StaticEmbedder()
    ).retrieve("shared note", _access(), corpus_version_id=snapshot.corpus_version_id)
    assert len(bundle.hits) == 1


def test_longer_excerpts_do_not_evict_required_sources_or_exceed_input_bound() -> None:
    items = tuple(
        _knowledge_evidence(
            marker=f"{index:x}",
            subject_id=f"product_{index}",
            text="Background note. " * 48 + f"Unique late detail {index}.",
        )[0]
        for index in range(1, 11)
    )
    evidence = ToolEvidence(
        excerpts=tuple(e for item in items for e in item.excerpts),
        references=tuple(r for item in items for r in item.references),
    )
    requirements = tuple(
        EvidenceRequirement(EvidenceKind.KNOWLEDGE, f"product_{index}")
        for index in range(1, 11)
    )
    instructions = "x" * 9_000
    schema = _bounded_model_answer_schema(evidence)
    payload = _bounded_model_input(
        evidence,
        requirements,
        instructions=instructions,
        schema=schema,
        envelope=lambda catalog: {"catalog": catalog},
    )
    assert _model_payload_token_bound(instructions, payload, schema) <= (
        GENERATION_INPUT_TOKEN_LIMIT
    )
    extracts = json.loads(payload)["catalog"]["extracts"]
    assert {e["e"] for e in extracts} == {e.evidence_id for e in evidence.excerpts}
    # The full text of all sources would exceed the same bound: compression
    # remains necessary here, rather than silently increasing the input budget.
    assert any(
        len(e["x"]) < len(original.exact_text)
        for e, original in zip(extracts, evidence.excerpts, strict=True)
    )
