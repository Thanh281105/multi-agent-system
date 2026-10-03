from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.contracts import TaskStatus
from app.knowledge.grounding import GroundingVerifier
from app.shared.budget import (
    BudgetCancelledError,
    ProviderBudgetContext,
    current_provider_budget,
    provider_budget_scope,
)
from app.v2.answers import GroundedAnswerProducer
from app.v2.contracts import EvidenceKind, EvidenceReference
from app.v2.execution import ModelRuntimeExpertReasoner
from app.v2.registry import ServiceId
from app.v2.runtime_contracts import (
    AnswerDraft,
    DraftClaim,
    EvidenceExcerpt,
    ExpertResult,
    RuntimeOperation,
    ToolEvidence,
    build_operation_key,
)


class _Runtime:
    def __init__(self) -> None:
        self.stages: list[str] = []

    async def generate_structured(self, **kwargs: Any) -> Any:
        self.stages.append(kwargs["stage"])
        return SimpleNamespace(
            value=AnswerDraft(
                claims=(DraftClaim(claim_id="claim_probe", fact_ids=("missing",)),)
            )
        )


def _evidence() -> ToolEvidence:
    reference = EvidenceReference(
        evidence_id="evidence_probe",
        source_id="source_probe",
        source_version_id="catalog_v1",
        chunk_id="chunk_probe",
        span_id="span_probe",
        display_label="[C1]",
        kind=EvidenceKind.CATALOG,
        title="Book Probe",
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    return ToolEvidence(
        references=(reference,),
        excerpts=(
            EvidenceExcerpt(
                evidence_id=reference.evidence_id,
                source_id=reference.source_id,
                source_version_id=reference.source_version_id,
                chunk_id=reference.chunk_id,
                span_id=reference.span_id,
                subject_ids=("product_1",),
                exact_text=(
                    "Book Probe explains orbital mechanics through worked examples."
                ),
            ),
        ),
    )


async def _run_stage(stage: str, runtime: _Runtime) -> None:
    evidence = _evidence()
    if stage == "expert":
        parameters = {"query": "Book Probe"}
        versions = ("catalog_v1",)
        operation = RuntimeOperation(
            step_id="step_probe",
            capability="product.catalog.search",
            service=ServiceId.PRODUCT,
            parameters=parameters,
            data_version_ids=versions,
            operation_key=build_operation_key(
                "product.catalog.search", parameters, versions
            ),
        )
        observed_at = evidence.references[0].observed_at
        result = ExpertResult(
            operation=operation,
            status=TaskStatus.SUCCESS,
            output={},
            evidence=evidence,
            started_at=observed_at,
            completed_at=observed_at,
        )
        await ModelRuntimeExpertReasoner(
            runtime, runtime_mode="required", model="fake"
        ).enrich(result)
    elif stage == "semantic":
        await GroundingVerifier(runtime, model="fake").verify(
            AnswerDraft(
                claims=(
                    DraftClaim(
                        claim_id="claim_probe",
                        text=(
                            "Book Probe uses worked examples to teach "
                            "orbital mechanics."
                        ),
                        evidence_ids=("evidence_probe",),
                    ),
                )
            ),
            evidence,
            allowed_subject_ids=frozenset({"product_1"}),
        )
    else:
        await GroundedAnswerProducer(
            runtime, runtime_mode="required", model="fake"
        ).produce(
            user_request="Explain Book Probe",
            evidence=evidence,
            allowed_subject_ids=frozenset({"product_1"}),
            allow_repair=stage == "repair",
        )


@pytest.mark.parametrize("stage", ["expert", "draft", "repair", "semantic"])
def test_slow_fresh_cancellation_probe_does_not_block_async_loop(stage: str) -> None:
    async def exercise() -> None:
        loop_thread = threading.get_ident()
        probe_threads: list[int] = []
        probe_scopes: list[str | None] = []
        slow_probe_started_at: list[float] = []
        heartbeat_delay: list[float] = []
        runtime = _Runtime()

        def cancelled() -> bool:
            probe_threads.append(threading.get_ident())
            current = current_provider_budget()
            probe_scopes.append(None if current is None else current.scope_id)
            if stage == "repair" and len(probe_threads) == 1:
                return False
            slow_probe_started_at.append(time.monotonic())
            time.sleep(0.2)
            return True

        async def heartbeat() -> None:
            while not slow_probe_started_at:
                await asyncio.sleep(0.001)
            heartbeat_delay.append(time.monotonic() - slow_probe_started_at[0])

        context = ProviderBudgetContext(
            ledger=SimpleNamespace(),
            scope_id="scope_probe",
            purpose="chat",
            cancellation_requested=cancelled,
        )
        with provider_budget_scope(context):
            heartbeat_task = asyncio.create_task(heartbeat())
            with pytest.raises(
                BudgetCancelledError, match="provider_dispatch_cancelled"
            ):
                await _run_stage(stage, runtime)
            await heartbeat_task

        expected_calls = 2 if stage == "repair" else 1
        assert len(probe_threads) == expected_calls
        assert probe_scopes == ["scope_probe"] * expected_calls
        assert runtime.stages == (["answer.draft"] if stage == "repair" else [])
        assert all(thread != loop_thread for thread in probe_threads), (
            f"{stage}: fresh probe ran on event-loop thread; "
            f"heartbeat delay={heartbeat_delay[0]:.3f}s"
        )
        assert heartbeat_delay[0] < 0.1, heartbeat_delay

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["expert", "draft", "repair", "semantic"])
def test_task_cancel_during_probe_never_dispatches_later_stage(stage: str) -> None:
    async def exercise() -> None:
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        probe_calls = 0
        runtime = _Runtime()

        def cancelled() -> bool:
            nonlocal probe_calls
            probe_calls += 1
            if stage == "repair" and probe_calls == 1:
                return False
            entered.set()
            try:
                assert release.wait(1), "probe worker was not released"
                return False
            finally:
                finished.set()

        context = ProviderBudgetContext(
            ledger=SimpleNamespace(),
            scope_id="scope_probe",
            purpose="chat",
            cancellation_requested=cancelled,
        )
        with provider_budget_scope(context):
            stage_task = asyncio.create_task(_run_stage(stage, runtime))
            try:
                async with asyncio.timeout(1):
                    while not entered.is_set():
                        await asyncio.sleep(0.001)
                stage_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await stage_task
            finally:
                release.set()
                assert await asyncio.to_thread(finished.wait, 1)

        assert runtime.stages == (["answer.draft"] if stage == "repair" else [])

    asyncio.run(exercise())
