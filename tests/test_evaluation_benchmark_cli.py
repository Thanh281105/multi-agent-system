"""Focused local-only operator boundary tests for Package 8."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
from pydantic import BaseModel

from app.evaluation import benchmark_cli
from app.evaluation.benchmark_evidence import CatalogReviewResolvedEvidenceV3
from app.evaluation.benchmark_reporting import (
    ReceiptAccountingV3,
    ReceiptFailureTaxonomyEntryV3,
)
from app.evaluation.v3_runner import EvaluationRunSummaryV3
from app.knowledge.v2_contracts import sha256_utf8
from app.v2.authorization import ResourceAuthorization, ResourceBinding
from app.v2.contracts import ConversationMode, EvidenceKind, EvidenceReference


class _Preparation(BaseModel):
    preparation_sha256: str
    schedule_sha256: str


class _CalibrationInputs(BaseModel):
    inputs_sha256: str


def test_operate_preserves_successor_authority_after_calibration(tmp_path, monkeypatch):
    from tests.test_evaluation_benchmark_finalization import _binding_inputs
    from tests.test_evaluation_benchmark_judge_successor import _successor_inputs

    inputs, binding = _binding_inputs(monkeypatch)
    row, _, _, _ = _successor_inputs()
    configuration, case, _, thresholds, record = row
    calibration_inputs = SimpleNamespace(
        cases=(case,),
        references=inputs["references"],
        thresholds=thresholds,
        expected_calibration_ids=(case.calibration_id,),
    )
    frozen = SimpleNamespace(
        successor_binding=binding,
        project_root=tmp_path,
        protocol_sha256=binding.protocol_sha256,
        protocol=object(),
        heldout_cases=(),
        loaded_gold=SimpleNamespace(gold=SimpleNamespace(source_assets=object())),
    )
    checkpoint = tmp_path / "completed-checkpoint.jsonl"
    checkpoint.write_bytes(b"synthetic-complete")
    paths = SimpleNamespace(
        checkpoint=checkpoint, plan=object(), schedule=object(), output=tmp_path
    )
    resources = SimpleNamespace(
        model_runtime=object(),
        ledger=object(),
        account_id="offline-test",
        knowledge_service=object(),
        corpus_version_id="test-corpus",
        index_manifest_id="test-index",
        catalog_snapshot_version_id="test-catalog",
    )
    replacements = {
        "_require_operator_guard": lambda *_: None,
        "_load_frozen": lambda *_: frozen,
        "_paths": lambda *_: paths,
        "_run_id": lambda *_: "offline-successor",
        "build_heldout_schedule_v3": lambda *_, **__: (),
        "build_heldout_execution_plan_v3": lambda *_, **__: object(),
        "_require_plan": lambda *_: None,
        "_require_schedule": lambda *_: None,
        "_load_complete_heldout_receipts": lambda **_: (),
        "_load_complete_pilot_receipts": lambda **_: ((), ()),
        "_load_pilot_cases": lambda *_, **__: (),
        "_receipt_evidence_authorities": lambda **_: {},
        "_build_live_operator_resources": lambda **_: resources,
        "_ReceiptExactEvidenceResolver": lambda **_: object(),
        "ImmutableCatalogReviewSourceV3": lambda **_: object(),
        "_load_independent_references": lambda *_: inputs["references"],
        "build_package8_pilot_calibration_inputs_v3": lambda **_: calibration_inputs,
        "prepare_heldout_judging_v3": lambda **_: inputs["preparation"],
        "_write_or_validate_preparation_artifacts": lambda *_, **__: None,
        "_load_preparation_artifacts": lambda *_: (
            inputs["preparation"],
            calibration_inputs,
        ),
        "_build_judge_configuration": lambda *_: configuration,
    }
    for name, replacement in replacements.items():
        monkeypatch.setattr(benchmark_cli, name, replacement)
    artifacts = {}
    monkeypatch.setattr(
        benchmark_cli,
        "_write_or_validate_model_artifact",
        lambda path, value, **_: artifacts.setdefault(path, value),
    )
    monkeypatch.setattr(
        benchmark_cli, "_load_model_artifact", lambda path, *_, **__: artifacts[path]
    )
    reached_heldout = []

    class Runner:
        def __init__(self, **kwargs):
            assert kwargs["successor_binding"] == binding

        async def run_development_calibration_cases(self, **kwargs):
            return (SimpleNamespace(status="completed", record=record),)

        async def run_heldout_packet(self, **kwargs):
            reached_heldout.append(True)
            # Stop before any scoring. Real calibration freeze and both run-key
            # guards have run; no model runtime method is reachable in this test.
            raise RuntimeError("stop_after_successor_bindings")

    monkeypatch.setattr(benchmark_cli, "DurableModelJudgeRunnerV3", Runner)
    arguments = SimpleNamespace(
        pilot_checkpoint=checkpoint,
        pilot_schedule=object(),
        p7_runtime_manifest=object(),
        database_url="offline",
        export_reference_cases=None,
        calibration_max_absolute_error=0.25,
        judge_budget_nano_usd=10_000_000_000,
    )
    with pytest.raises(RuntimeError, match="stop_after_successor_bindings"):
        benchmark_cli._operate(arguments)
    assert reached_heldout == [True]


def _artifact_pair(*, marker: str) -> tuple[_Preparation, _CalibrationInputs]:
    return (
        _Preparation(preparation_sha256=marker * 64, schedule_sha256="b" * 64),
        _CalibrationInputs(inputs_sha256="c" * 64),
    )


def test_preparation_artifacts_are_immutable_and_detect_drift(tmp_path: Path) -> None:
    directory = tmp_path / "p8-preparation"
    preparation, calibration = _artifact_pair(marker="a")

    benchmark_cli._write_or_validate_preparation_artifacts(
        directory,
        preparation=preparation,  # type: ignore[arg-type]
        calibration_inputs=calibration,  # type: ignore[arg-type]
    )
    benchmark_cli._write_or_validate_preparation_artifacts(
        directory,
        preparation=preparation,  # type: ignore[arg-type]
        calibration_inputs=calibration,  # type: ignore[arg-type]
    )

    changed, _ = _artifact_pair(marker="d")
    with pytest.raises(benchmark_cli.BenchmarkCLIError) as caught:
        benchmark_cli._write_or_validate_preparation_artifacts(
            directory,
            preparation=changed,  # type: ignore[arg-type]
            calibration_inputs=calibration,  # type: ignore[arg-type]
        )
    assert caught.value.code == "preparation_artifact_drift"


def test_completed_heldout_checkpoint_is_opened_only_with_forbidden_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _Runner:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

    monkeypatch.setattr(benchmark_cli, "HeldoutEvaluationV3ObservationRunner", _Runner)
    monkeypatch.setattr(
        benchmark_cli,
        "_read_checkpoint_result",
        lambda *_args, **_kwargs: SimpleNamespace(
            summary=SimpleNamespace(
                total_scheduled=1,
                completed_turn_ids=("turn_1",),
                failed_turn_ids=(),
                ambiguous_turn_ids=(),
                orphan_started_turn_ids=(),
                missing_turn_ids=(),
            ),
            receipts=("receipt",),
        ),
    )

    receipts = benchmark_cli._load_complete_heldout_receipts(
        frozen=SimpleNamespace(protocol_sha256="a" * 64),  # type: ignore[arg-type]
        schedule=(SimpleNamespace(),),  # type: ignore[arg-type]
        checkpoint=tmp_path / "already-complete.jsonl",
    )

    assert receipts == ("receipt",)
    assert isinstance(observed["executor_factory"], benchmark_cli._ForbiddenFactory)


def test_partial_report_opens_only_the_existing_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "heldout-checkpoint.v3.jsonl"
    checkpoint.write_bytes(b"checkpoint")
    summary = EvaluationRunSummaryV3(
        run_id="run_partial_report",
        protocol_sha256="a" * 64,
        execution_case_set_sha256="b" * 64,
        schedule_sha256="c" * 64,
        total_scheduled=1,
        completed_turn_ids=(),
        failed_turn_ids=("turn_failed",),
        pending_turn_ids=(),
        missing_turn_ids=(),
        ambiguous_turn_ids=(),
        orphan_started_turn_ids=(),
        completed_observation_ids=(),
        is_partial=True,
        blocked_on_ambiguous_work=False,
    )
    accounting = ReceiptAccountingV3(
        receipt_count=1,
        completed_measured_receipt_count=0,
        failed_receipt_count=1,
        warmup_receipt_count=0,
        known_cost_usd=0,
        unresolved_reserved_cost_usd=0,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        generation_call_count=0,
        embedding_call_count=0,
        provider_attempt_count=0,
        retry_count=0,
        failures=(
            ReceiptFailureTaxonomyEntryV3(
                safe_error_code="turn_execution_failed",
                receipt_count=1,
            ),
        ),
    )
    frozen = SimpleNamespace(
        protocol_sha256="a" * 64,
        repeat_decision_sha256="d" * 64,
    )
    schedule = (SimpleNamespace(),)
    live_factory_called = False

    class _Runner:
        def __init__(self, **kwargs: object) -> None:
            assert isinstance(
                kwargs["executor_factory"], benchmark_cli._ForbiddenFactory
            )

    def _unexpected_live_factory(**_: object) -> object:
        nonlocal live_factory_called
        live_factory_called = True
        raise AssertionError("partial report must never construct live services")

    monkeypatch.setattr(benchmark_cli, "_load_frozen", lambda _: frozen)
    monkeypatch.setattr(
        benchmark_cli, "build_heldout_schedule_v3", lambda *_args, **_kwargs: schedule
    )
    monkeypatch.setattr(benchmark_cli, "pilot_schedule_sha256_v3", lambda _: "c" * 64)
    monkeypatch.setattr(
        benchmark_cli,
        "_load_model_artifact",
        lambda *_args, **_kwargs: SimpleNamespace(
            package7_protocol_sha256="a" * 64,
            repeat_decision_sha256="d" * 64,
            heldout_schedule_sha256="c" * 64,
        ),
    )
    monkeypatch.setattr(benchmark_cli, "_require_schedule", lambda *_args: None)
    monkeypatch.setattr(benchmark_cli, "HeldoutEvaluationV3ObservationRunner", _Runner)
    monkeypatch.setattr(
        benchmark_cli,
        "_read_checkpoint_result",
        lambda *_args, **_kwargs: SimpleNamespace(summary=summary, receipts=()),
    )
    monkeypatch.setattr(
        benchmark_cli, "account_observation_receipts_v3", lambda _: accounting
    )
    monkeypatch.setattr(
        benchmark_cli,
        "_build_package7_live_executor_factory",
        _unexpected_live_factory,
    )

    assert (
        benchmark_cli.cli(
            (
                "partial-report",
                "--project-root",
                str(tmp_path),
                "--output",
                str(tmp_path),
                "--checkpoint",
                str(checkpoint),
                "--run-id",
                "run_partial_report",
            )
        )
        == 3
    )
    assert not live_factory_called
    assert (tmp_path / "partial-execution-report.p8.json").is_file()


def test_local_prepare_rejects_citations_without_exact_source_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(citations=(SimpleNamespace(),))
    durable = SimpleNamespace(result=result)
    monkeypatch.setattr(
        benchmark_cli.DurableTurnOutcome,
        "model_validate",
        lambda _: durable,
    )
    receipt = SimpleNamespace(
        turn_results=(SimpleNamespace(result_payload={"status": "completed"}),)
    )

    with pytest.raises(benchmark_cli.BenchmarkCLIError) as caught:
        benchmark_cli._reject_unresolved_local_citations((receipt,))  # type: ignore[arg-type]
    assert caught.value.code == "citation_authority_required"


def test_run_requires_network_consent_before_live_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _unexpected_factory(**_: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("live factory must not be reached without consent")

    monkeypatch.setattr(
        benchmark_cli, "_build_package7_live_executor_factory", _unexpected_factory
    )
    frozen = SimpleNamespace(protocol_sha256="a" * 64, protocol=object())
    schedule = (SimpleNamespace(),)
    monkeypatch.setattr(benchmark_cli, "_load_frozen", lambda _: frozen)
    monkeypatch.setattr(
        benchmark_cli,
        "build_heldout_schedule_v3",
        lambda *_args, **_kwargs: schedule,
    )
    monkeypatch.setattr(
        benchmark_cli,
        "build_heldout_execution_plan_v3",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    assert (
        benchmark_cli.cli(
            (
                "run",
                "--project-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "heldout"),
            )
        )
        == 2
    )
    assert not called


def test_package7_runtime_url_pins_utc_and_preserves_connection_options() -> None:
    source = (
        "postgresql+psycopg://operator@example.invalid:5432/evaluation"
        "?application_name=p8&options=-c%20search_path%3Devaluation"
    )

    rewritten = benchmark_cli._with_utc_session_timezone(source)
    parsed = urlsplit(rewritten)
    query = parse_qsl(parsed.query, keep_blank_values=True)

    assert parsed.scheme == "postgresql+psycopg"
    assert parsed.netloc == "operator@example.invalid:5432"
    assert parsed.path == "/evaluation"
    assert query == [
        ("application_name", "p8"),
        ("options", "-c search_path=evaluation -c TimeZone=UTC"),
    ]


def test_operate_requires_network_consent_before_loading_any_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = False

    def _unexpected_load(_: object) -> object:
        nonlocal loaded
        loaded = True
        raise AssertionError("frozen inputs must not be loaded before network consent")

    monkeypatch.setattr(benchmark_cli, "_load_frozen", _unexpected_load)

    assert (
        benchmark_cli.cli(
            (
                "operate",
                "--project-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "heldout"),
                "--database-url",
                "postgresql://operator:placeholder@localhost/evaluation",
                "--judge-budget-nano-usd",
                "1",
            )
        )
        == 2
    )
    assert not loaded


def test_exact_resolver_refuses_missing_authority_without_service_call() -> None:
    resolver = benchmark_cli._ReceiptExactEvidenceResolver(
        knowledge_service=object(),
        corpus_version_id="cor_" + "a" * 60,
        index_manifest_id="idx_" + "b" * 60,
        authorities={},
    )
    binding = benchmark_cli.EvidenceBindingKeyV3(
        evidence_id="evidence_missing",
        source_id="source_missing",
        source_version_id="version_missing",
        chunk_id="chunk_missing",
        span_id="span_missing",
    )

    with pytest.raises(benchmark_cli.BenchmarkCLIError) as caught:
        resolver.resolve(binding)
    assert caught.value.code == "exact_evidence_authority_unavailable"


@pytest.mark.parametrize(
    "kind", (EvidenceKind.CATALOG, EvidenceKind.REVIEW, EvidenceKind.TRUST)
)
def test_exact_resolver_validates_every_receipt_authority_for_shared_public_record(
    kind: EvidenceKind,
) -> None:
    source_id = {
        EvidenceKind.CATALOG: "catalog_product_1",
        EvidenceKind.REVIEW: "review_sample_1",
        EvidenceKind.TRUST: "trust_sample_1",
    }[kind]
    binding = benchmark_cli.EvidenceBindingKeyV3(
        evidence_id="evidence_catalog",
        source_id=source_id,
        source_version_id="cat_" + "c" * 64,
    )
    reference = EvidenceReference(
        **binding.model_dump(),
        display_label="[C1]",
        kind=kind,
        title="Immutable public record",
        observed_at=datetime(2026, 9, 15, tzinfo=UTC),
    )
    accesses = tuple(
        ResourceAuthorization(
            binding=ResourceBinding(
                tenant_id=f"tenant_{name}",
                principal_id=f"principal_{name}",
                store_id="demo",
                mode=ConversationMode.SHOPPER,
            ),
            scopes=frozenset({"ecommerce.read"}),
        )
        for name in ("first", "second")
    )
    seen = []

    class Source:
        def reopen(self, ref, access):
            seen.append(access)
            return CatalogReviewResolvedEvidenceV3(
                binding=binding,
                reference=ref,
                authorization=access,
                exact_text="Immutable source text",
                content_sha256=sha256_utf8("Immutable source text"),
            )

    resolver = benchmark_cli._ReceiptExactEvidenceResolver(
        knowledge_service=object(),
        corpus_version_id="cor_" + "a" * 60,
        index_manifest_id="idx_" + "b" * 60,
        authorities={
            binding: tuple(
                benchmark_cli._ReceiptEvidenceAuthority(
                    reference=reference.model_copy(
                        update={"display_label": f"[C{index}]"}
                    ),
                    authorization=access,
                )
                for index, access in enumerate(accesses, start=1)
            )
        },
        catalog_review_source=Source(),  # type: ignore[arg-type]
    )
    assert resolver.resolve(binding).exact_text == "Immutable source text"
    assert seen == list(accesses)


def test_exact_resolver_catalog_without_immutable_assets_remains_blocked() -> None:
    binding = benchmark_cli.EvidenceBindingKeyV3(
        evidence_id="evidence_catalog",
        source_id="catalog_product_1",
        source_version_id="cat_" + "c" * 64,
    )
    reference = EvidenceReference(
        **binding.model_dump(),
        display_label="[C1]",
        kind=EvidenceKind.CATALOG,
        title="Do not use this as evidence",
        observed_at=datetime(2026, 9, 15, tzinfo=UTC),
    )
    access = ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id="tenant_first",
            principal_id="principal_first",
            store_id="demo",
            mode=ConversationMode.SHOPPER,
        ),
        scopes=frozenset({"ecommerce.read"}),
    )
    resolver = benchmark_cli._ReceiptExactEvidenceResolver(
        knowledge_service=object(),
        corpus_version_id="cor_" + "a" * 60,
        index_manifest_id="idx_" + "b" * 60,
        authorities={
            binding: benchmark_cli._ReceiptEvidenceAuthority(reference, access)
        },
    )
    with pytest.raises(benchmark_cli.BenchmarkCLIError) as caught:
        resolver.resolve(binding)
    assert caught.value.code == "generic_exact_authority_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("none", "namespace", "state", "missing", "warmup"))
async def test_seeded_price_requires_native_reset_and_skips_warmups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    from app.evaluation.benchmark_evidence import ImmutableCatalogReviewSourceV3
    from app.evaluation.v3_models import ScheduledTurnKindV3
    from app.evaluation.v3_runner import run_observations_v3
    from tests.test_evaluation_benchmark_evidence import (
        SANDBOX_SOURCE_VERSION_ID,
        _public_asset_bindings,
        _public_runtime_evidence,
    )
    from tests.test_evaluation_v3_runner import (
        _cases,
        _protocol,
        _RecordingFactory,
        _schedule,
    )

    protocol = _protocol()
    kind = (
        ScheduledTurnKindV3.WARMUP
        if tamper == "warmup"
        else ScheduledTurnKindV3.MEASURED
    )
    schedule = _schedule(
        protocol, ((kind, "sa_shared_tools_rag", protocol.pilot_cases[0].case_id, 0),)
    )
    cases = _cases(protocol, schedule)
    run = await run_observations_v3(
        protocol=protocol,
        schedule=schedule,
        cases=cases,
        checkpoint_path=tmp_path / "checkpoint.jsonl",
        executor_factory=_RecordingFactory(),
    )
    receipt = run.receipts[0]
    reset = receipt.initial_state_reset_receipt
    assert reset is not None
    if tamper in {"namespace", "state"}:
        reset = reset.model_copy(
            update={
                "namespace_id" if tamper == "namespace" else "initial_state_sha256": (
                    "namespace_" + "0" * 64 if tamper == "namespace" else "0" * 64
                )
            }
        )
    elif tamper == "missing":
        reset = None
    receipt = receipt.model_copy(update={"initial_state_reset_receipt": reset})
    reference, text = _public_runtime_evidence(EvidenceKind.SANDBOX, product_id=80)
    seen = []

    def final_result(_):
        seen.append(True)
        return SimpleNamespace(
            citations=(SimpleNamespace(evidence_id=reference.evidence_id),),
            evidence=(reference,),
        )

    monkeypatch.setattr(benchmark_cli, "_receipt_final_result", final_result)
    if tamper in {"namespace", "state", "missing"}:
        with pytest.raises(benchmark_cli.BenchmarkCLIError) as caught:
            benchmark_cli._receipt_evidence_authorities(
                batches=((schedule, (receipt,), cases),)
            )
        assert caught.value.code == "sandbox_fixture_authority_invalid"
        return
    authorities = benchmark_cli._receipt_evidence_authorities(
        batches=((schedule, (receipt,), cases),)
    )
    if tamper == "warmup":
        assert authorities == {} and not seen
        return
    assert len(authorities) == 1
    authority = next(iter(authorities.values()))[0]
    assert authority.catalog_seeded_offer and authority.sandbox_evidence is None
    root, bindings = _public_asset_bindings()
    resolver = benchmark_cli._ReceiptExactEvidenceResolver(
        knowledge_service=object(),
        corpus_version_id="cor_" + "a" * 60,
        index_manifest_id="idx_" + "b" * 60,
        authorities=authorities,
        catalog_review_source=ImmutableCatalogReviewSourceV3(
            project_root=root,
            bindings=bindings,
            snapshot_version_id=SANDBOX_SOURCE_VERSION_ID,
        ),
    )
    assert resolver.resolve(next(iter(authorities))).exact_text == text
