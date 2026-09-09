"""Fixture-only regressions for the Package 3 retrieval calibration runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.knowledge.v2_contracts import (
    AuthorizedKnowledgeSource,
    IndexBuildSpec,
    KnowledgeSpan,
    PublishedKnowledgeSnapshot,
    RetrievalChunk,
    RetrievalPolicy,
    SourceSupportScope,
    content_addressed_id,
    sha256_text,
    sha256_utf8,
    stable_chunk_id,
    stable_span_id,
)
from app.shared.budget import BudgetCostSummary, ScopeUsageSummary
from app.v2.authorization import ResourceAuthorization, ResourceBinding
from app.v2.contracts import ConversationMode
from scripts import calibrate_book_retrieval as calibration

NOW = datetime(2026, 9, 9, tzinfo=UTC)
MODEL = "test-embedding-v1"
DIMENSIONS = 32


def _vector(axis: int = 0) -> tuple[float, ...]:
    return tuple(1.0 if index == axis else 0.0 for index in range(DIMENSIONS))


def _policy(version: str = "hybrid_v1", **updates: object) -> RetrievalPolicy:
    payload: dict[str, object] = {
        "version": version,
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


def _snapshot(
    label: str, *, policy: RetrievalPolicy | None = None
) -> PublishedKnowledgeSnapshot:
    spec = IndexBuildSpec(
        embedding_model=MODEL,
        embedding_dimension=DIMENSIONS,
        chunker_version="table_chunker_v1",
        enrichment_policy_version="source_enrichment_v1",
    )
    return PublishedKnowledgeSnapshot(
        corpus_version_id=content_addressed_id("cor", {"corpus": label}),
        corpus_name="books-v1",
        corpus_version=label,
        index_manifest_id=content_addressed_id("idx", {"index": label}),
        index_fingerprint=spec.index_fingerprint,
        embedding_model=spec.embedding_model,
        embedding_dimension=spec.embedding_dimension,
        chunker_version=spec.chunker_version,
        enrichment_policy_version=spec.enrichment_policy_version,
        query_embedding_fingerprint=spec.query_embedding_fingerprint,
        retrieval_policy=policy or _policy(),
        published_at=NOW,
    )


def _sources() -> tuple[AuthorizedKnowledgeSource, ...]:
    rows = (
        (
            "src_sapiens_author",
            "Sapiens",
            "Yuval Noah Harari history of humankind",
            "work_sapiens",
        ),
        (
            "src_de_men_kim_dong",
            "Dế Mèn phiêu lưu ký",
            "Tô Hoài themes and journey",
            "work_de_men",
        ),
        (
            "src_zero_to_one_prh",
            "Không Đến Một",
            "Peter Thiel startup and new value",
            "work_zero_to_one",
        ),
        (
            "src_flour_water_salt_yeast_prh",
            "Bột Nước Muối Men",
            "Ken Forkish bread and pizza baking",
            "work_flour_water_salt_yeast",
        ),
    )
    return tuple(
        AuthorizedKnowledgeSource(
            source_id=source_id,
            source_version_id=content_addressed_id("svr", {"source_id": source_id}),
            title=title,
            url=f"https://example.test/{source_id}",
            planning_text=planning_text,
            keywords=tuple(planning_text.split()),
            support_scope=SourceSupportScope.WORK,
            work_identifier=work_identifier,
            edition_identifier=None,
            retrieved_at=NOW,
        )
        for source_id, title, planning_text, work_identifier in rows
    )


def _chunks(
    snapshot: PublishedKnowledgeSnapshot,
    sources: tuple[AuthorizedKnowledgeSource, ...],
) -> tuple[RetrievalChunk, ...]:
    chunks = []
    for index, source in enumerate(sources):
        content = f"{source.title}. {source.planning_text}."
        content_hash = sha256_text(content)
        chunk_id = stable_chunk_id(
            source_version_id=source.source_version_id,
            chunker_version=snapshot.chunker_version,
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
        chunks.append(
            RetrievalChunk(
                corpus_version_id=snapshot.corpus_version_id,
                index_manifest_id=snapshot.index_manifest_id,
                source_id=source.source_id,
                source_version_id=source.source_version_id,
                chunk_id=chunk_id,
                chunk_index=0,
                chunker_version=snapshot.chunker_version,
                title=source.title,
                url=source.url,
                content=content,
                token_count=16,
                content_hash=content_hash,
                vector=_vector(index),
                spans=(span,),
                support_scope=source.support_scope,
                work_identifier=source.work_identifier,
                edition_identifier=source.edition_identifier,
            )
        )
    return tuple(chunks)


def _access() -> ResourceAuthorization:
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="default",
            principal_id="calibration-admin",
            mode=ConversationMode.SHOPPER,
            store_id="demo",
        ),
        scopes=frozenset({"ecommerce.read"}),
    )


class FakeStore:
    def __init__(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        sources: tuple[AuthorizedKnowledgeSource, ...],
        chunks: tuple[RetrievalChunk, ...],
    ) -> None:
        self.snapshot = snapshot
        self.sources = sources
        self.chunks = chunks
        self.events: list[str] = []

    def resolve_published_snapshot(
        self, corpus_version_id: str, *, index_manifest_id: str | None = None
    ) -> PublishedKnowledgeSnapshot:
        self.events.append("snapshot")
        assert corpus_version_id == self.snapshot.corpus_version_id
        assert index_manifest_id == self.snapshot.index_manifest_id
        return self.snapshot

    def list_authorized_sources(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        product_ids: tuple[int, ...] = (),
    ) -> tuple[AuthorizedKnowledgeSource, ...]:
        del access, product_ids
        self.events.append("sources")
        assert snapshot.corpus_version_id == self.snapshot.corpus_version_id
        return self.sources

    def load_authorized_chunks(
        self,
        snapshot: PublishedKnowledgeSnapshot,
        access: ResourceAuthorization,
        *,
        source_ids: frozenset[str],
    ) -> tuple[RetrievalChunk, ...]:
        del access
        self.events.append("chunks")
        assert snapshot.corpus_version_id == self.snapshot.corpus_version_id
        return tuple(chunk for chunk in self.chunks if chunk.source_id in source_ids)

    def resolve_authorized_evidence(self, *_: object, **__: object) -> Any:
        raise AssertionError("calibration never reopens evidence")


class FakeLedger:
    def __init__(self) -> None:
        self.provider_attempts = 0
        self.input_tokens = 0
        self.unknown_nano_usd = 0
        self.unknown_cost_attempts = 0
        self.attempts_without_usage = 0

    def scope_usage_summary(self, scope_id: str) -> ScopeUsageSummary:
        assert scope_id == "p3-calibration"
        return ScopeUsageSummary(
            costs=BudgetCostSummary(
                known_nano_usd=100 * self.provider_attempts,
                reserved_nano_usd=0,
                unknown_nano_usd=self.unknown_nano_usd,
                hard_limit_nano_usd=5_000_000_000,
            ),
            input_tokens=self.input_tokens,
            cached_input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            total_tokens=self.input_tokens,
            provider_attempts=self.provider_attempts,
            pending_attempts=0,
            unknown_cost_attempts=self.unknown_cost_attempts,
            attempts_without_usage=self.attempts_without_usage,
        )

    def record_provider_call(self, query_count: int) -> None:
        self.provider_attempts += 1
        self.input_tokens += query_count


class FakeEmbedder:
    model = MODEL
    method = MODEL
    dimensions = DIMENSIONS

    def __init__(self, ledger: FakeLedger) -> None:
        self.ledger = ledger
        self.calls: list[list[str]] = []

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        self.ledger.record_provider_call(len(texts))
        return [list(_vector()) for _ in texts]


def _prepared(
    label: str = "collection",
) -> tuple[
    FakeStore,
    calibration.PreparedCalibration,
    calibration.DevelopmentProbeManifest,
]:
    probes, probe_sha = calibration.read_fixed_probe_manifest()
    snapshot = _snapshot(label)
    sources = _sources()
    store = FakeStore(snapshot, sources, _chunks(snapshot, sources))
    prepared = calibration.prepare_calibration(
        store,
        probes,
        probe_sha,
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
        index_manifest_id=snapshot.index_manifest_id,
    )
    assert store.events[:2] == ["snapshot", "sources"]
    return store, prepared, probes


def _collect(
    tmp_path: Path,
    prepared: calibration.PreparedCalibration,
) -> tuple[Path, FakeLedger, FakeEmbedder, calibration.CalibrationCache]:
    cache_path = tmp_path / "calibration-cache.json"
    ledger = FakeLedger()
    embedder = FakeEmbedder(ledger)
    result = calibration.collect_query_embeddings(
        prepared.manifest,
        cache_path,
        budget_account_id="shared",
        budget_scope_id="p3-calibration",
        ledger=ledger,
        embedder_factory=lambda: embedder,
    )
    assert not result.reused
    return cache_path, ledger, embedder, result.cache


def test_complete_cache_reuse_makes_no_second_provider_call(tmp_path: Path) -> None:
    _, prepared, _ = _prepared()
    cache_path, ledger, embedder, first_cache = _collect(tmp_path, prepared)

    def unexpected_factory() -> FakeEmbedder:
        raise AssertionError("matching complete cache must not construct an embedder")

    second = calibration.collect_query_embeddings(
        prepared.manifest,
        cache_path,
        budget_account_id="shared",
        budget_scope_id="p3-calibration",
        ledger=ledger,
        embedder_factory=unexpected_factory,
    )

    assert second.reused
    assert second.cache == first_cache
    assert len(embedder.calls) == 1
    assert embedder.calls[0] == list(prepared.manifest.unique_queries)
    assert ledger.provider_attempts == 1


def test_successful_retry_with_earlier_unknown_usage_remains_reusable(
    tmp_path: Path,
) -> None:
    store, prepared, probes = _prepared()
    cache_path = tmp_path / "retry-cache.json"
    ledger = FakeLedger()

    class SuccessfulRetryEmbedder(FakeEmbedder):
        def embed_many(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            self.ledger.provider_attempts += 2
            self.ledger.input_tokens += len(texts)
            self.ledger.unknown_nano_usd += 500
            self.ledger.unknown_cost_attempts += 1
            self.ledger.attempts_without_usage += 1
            return [list(_vector()) for _ in texts]

    embedder = SuccessfulRetryEmbedder(ledger)
    collected = calibration.collect_query_embeddings(
        prepared.manifest,
        cache_path,
        budget_account_id="shared",
        budget_scope_id="p3-calibration",
        ledger=ledger,
        embedder_factory=lambda: embedder,
    )
    output = calibration.evaluate_policy_candidates(
        store,
        prepared,
        probes,
        _access(),
        collected.cache,
        (_policy("candidate_v1"),),
    )

    assert collected.cache.status == "complete"
    assert output["collection"]["usage_fully_known"] is False
    assert output["collection"]["usage_delta"]["unknown_cost_attempts"] == 1
    assert output["collection"]["usage_delta"]["attempts_without_usage"] == 1
    assert output["collection"]["vector_payload_sha256"]
    assert output["collection"]["cache_sha256"]

    reused = calibration.collect_query_embeddings(
        prepared.manifest,
        cache_path,
        budget_account_id="shared",
        budget_scope_id="p3-calibration",
        ledger=ledger,
        embedder_factory=lambda: (_ for _ in ()).throw(
            AssertionError("complete payload must not redispatch")
        ),
    )
    assert reused.reused
    assert len(embedder.calls) == 1


def test_mismatched_probe_or_query_fingerprint_is_rejected(tmp_path: Path) -> None:
    store, prepared, probes = _prepared()
    _, _, _, cache = _collect(tmp_path, prepared)
    candidates = (_policy("candidate_v1"),)

    for updates in (
        {"probe_manifest_sha256": "f" * 64},
        {"query_embedding_fingerprint": "e" * 64},
    ):
        mismatch = calibration.PreparedCalibration(
            manifest=prepared.manifest.model_copy(update=updates),
            snapshot=prepared.snapshot,
        )
        with pytest.raises(calibration.CalibrationError, match="identity"):
            calibration.evaluate_policy_candidates(
                store, mismatch, probes, _access(), cache, candidates
            )


def test_pending_claim_never_redispatches(tmp_path: Path) -> None:
    _, prepared, _ = _prepared()
    cache_path = tmp_path / "pending-cache.json"
    ledger = FakeLedger()

    class FailingEmbedder(FakeEmbedder):
        def embed_many(self, texts: list[str]) -> list[list[float]]:
            del texts
            raise RuntimeError("simulated transport interruption")

    with pytest.raises(RuntimeError, match="transport interruption"):
        calibration.collect_query_embeddings(
            prepared.manifest,
            cache_path,
            budget_account_id="shared",
            budget_scope_id="p3-calibration",
            ledger=ledger,
            embedder_factory=lambda: FailingEmbedder(ledger),
        )
    assert json.loads(cache_path.read_text(encoding="utf-8"))["status"] == "pending"

    def unexpected_factory() -> FakeEmbedder:
        raise AssertionError("pending claim must be inspected before provider setup")

    with pytest.raises(calibration.CalibrationError, match="redispatch is forbidden"):
        calibration.collect_query_embeddings(
            prepared.manifest,
            cache_path,
            budget_account_id="shared",
            budget_scope_id="p3-calibration",
            ledger=ledger,
            embedder_factory=unexpected_factory,
        )
    assert ledger.provider_attempts == 0


def test_planned_query_strings_are_first_seen_deduplicated() -> None:
    probes, _ = calibration.read_fixed_probe_manifest()
    repeated = probes.model_copy(
        update={
            "probes": tuple(
                probe.model_copy(update={"query": "one repeated query"})
                for probe in probes.probes
            )
        }
    )
    snapshot = _snapshot("dedup")
    sources = _sources()
    store = FakeStore(snapshot, sources, _chunks(snapshot, sources))

    prepared = calibration.prepare_calibration(
        store,
        repeated,
        "a" * 64,
        _access(),
        corpus_version_id=snapshot.corpus_version_id,
        index_manifest_id=snapshot.index_manifest_id,
    )

    assert prepared.manifest.unique_queries == ("one repeated query",)
    assert all(
        plan.planned_queries == ("one repeated query",)
        for plan in prepared.manifest.planned_probes
    )


def test_probe_identity_is_independent_of_json_formatting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(calibration._PROBES_PATH.read_text(encoding="utf-8"))
    identities = []
    for name, text in (
        ("lf.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n"),
        (
            "crlf.json",
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).replace("\n", "\r\n"),
        ),
    ):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8", newline="")
        monkeypatch.setattr(calibration, "_PROBES_PATH", path)
        _, identity = calibration.read_fixed_probe_manifest()
        identities.append(identity)

    assert identities == [calibration._FIXED_PROBE_MANIFEST_SHA256] * 2


@pytest.mark.parametrize("changed_field", ["query", "expected_source_ids"])
def test_probe_gold_mutation_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    payload = json.loads(calibration._PROBES_PATH.read_text(encoding="utf-8"))
    if changed_field == "query":
        payload["probes"][0]["query"] = "A changed gold query"
    else:
        payload["probes"][0]["expected_source_ids"] = ["src_changed_gold"]
    path = tmp_path / f"changed-{changed_field}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(calibration, "_PROBES_PATH", path)

    with pytest.raises(calibration.CalibrationError, match="fingerprint changed"):
        calibration.read_fixed_probe_manifest()


def test_offline_candidates_reuse_vectors_across_new_published_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, collection, probes = _prepared("collection")
    _, _, _, cache = _collect(tmp_path, collection)
    evaluation_store, evaluation, _ = _prepared("evaluation")
    snapshot_before = evaluation.snapshot.model_dump(mode="json")

    def provider_forbidden(*_: object, **__: object) -> None:
        raise AssertionError("offline evaluation must not construct a live provider")

    monkeypatch.setattr(calibration, "OpenAIEmbeddingRuntime", provider_forbidden)
    output = calibration.evaluate_policy_candidates(
        evaluation_store,
        evaluation,
        probes,
        _access(),
        cache,
        (
            _policy("candidate_low", min_dense_relevance=0.1),
            _policy("candidate_high", min_dense_relevance=0.8),
        ),
    )

    assert output["counterfactual_only"] is True
    assert output["published_database_mutated"] is False
    assert len(output["candidates"]) == 2
    assert (
        output["collection_snapshot"]["corpus_version_id"]
        != output["evaluation_snapshot"]["corpus_version_id"]
    )
    assert (
        output["evaluation_snapshot"]["stored_policy"]
        == snapshot_before["retrieval_policy"]
    )
    assert evaluation.snapshot.model_dump(mode="json") == snapshot_before
    assert all(
        row["query"] in {probe.query for probe in probes.probes}
        for candidate in output["candidates"]
        for row in candidate["probes"]
    )


@pytest.mark.parametrize("corruption", ["dimension", "nonfinite"])
def test_invalid_cached_vectors_fail_closed(tmp_path: Path, corruption: str) -> None:
    _, prepared, _ = _prepared()
    cache_path, _, _, _ = _collect(tmp_path, prepared)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    if corruption == "dimension":
        payload["vectors"][0]["vector"].append(0.0)
    else:
        payload["vectors"][0]["vector"][0] = float("nan")
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(calibration.CalibrationError, match="malformed"):
        calibration._read_cache(cache_path)


def test_finite_vector_tampering_breaks_payload_digest(tmp_path: Path) -> None:
    _, prepared, _ = _prepared()
    cache_path, _, _, _ = _collect(tmp_path, prepared)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["vectors"][0]["vector"][0] = 0.25
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(calibration.CalibrationError, match="malformed"):
        calibration._read_cache(cache_path)


def test_cli_parsing_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    parser = calibration._parser()
    args = parser.parse_args(
        [
            "collect",
            "--database-url",
            "postgresql+psycopg://fixture.invalid/test",
            "--corpus-version-id",
            "cor_" + "a" * 64,
            "--index-manifest-id",
            "idx_" + "b" * 64,
            "--tenant-id",
            "default",
            "--principal-id",
            "calibration-admin",
            "--mode",
            "shopper",
            "--access-scope",
            "ecommerce.read",
            "--cache",
            "cache.json",
            "--budget-account-id",
            "shared",
            "--budget-scope-id",
            "p3-calibration",
        ]
    )
    assert args.command == "collect"
    assert args.api_key_env == "OPENAI_API_KEY"

    with pytest.raises(SystemExit) as help_exit:
        calibration.main(["--help"])
    assert help_exit.value.code == 0
    assert "does not publish a policy" in capsys.readouterr().out
