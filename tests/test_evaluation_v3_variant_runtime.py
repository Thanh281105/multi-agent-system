"""Focused tests for truthful Package 7 runtime composition."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.contracts import AuthorizationContext
from app.evaluation.v3_executor import (
    EvaluationV3ObservationExecutor,
    EvaluationV3ObservationExecutorFactory,
)
from app.evaluation.v3_models import AgentTopologyV3, PlanningModeV3
from app.evaluation.v3_variant_runtime import (
    CONTROLLER_ACTOR_ID_V3,
    PACKAGE7_VARIANT_POLICIES,
    PINNED_GENERATION_MODEL_V3,
    EvaluationV3ModelCallRecorder,
    EvaluationV3RuntimeConfigurationError,
    EvaluationV3VariantComposer,
    EvaluationV3VariantPolicy,
    package7_variant_policy,
)
from app.knowledge.retrieval import (
    HybridKnowledgeRetriever,
    ModelRuntimeKnowledgeQueryPlanner,
)
from app.knowledge.service import KnowledgeService
from app.knowledge.v2_contracts import (
    AuthorizedKnowledgeSource,
    IndexBuildSpec,
    PublishedKnowledgeSnapshot,
    RetrievalPolicy,
    SourceSupportScope,
    content_addressed_id,
)
from app.shared import (
    ModelCallMetadata,
    ModelRuntime,
    StructuredModelResult,
    collect_model_calls,
    mark_model_call_fallback,
)
from app.shared.budget import ProviderBudgetContext, provider_budget_scope
from app.v2.answers import GroundedAnswerProducer
from app.v2.authorization import bind_request_authorization
from app.v2.contracts import ConversationMode, DialogueOutcome
from app.v2.execution import (
    DurableExecutionError,
    DurableOperationExecutor,
    DurableReadTurnExecutor,
    OperationBatch,
)
from app.v2.planning import (
    BoundedV2Planner,
    PlannedTurn,
    PlanningContext,
    RuntimeDataVersions,
)
from app.v2.registry import default_v2_registry
from app.v2.runtime import V2ModelSnapshot, V2ServiceGraph
from app.v2.runtime_contracts import GroundingResult, RuntimeOperation
from app.v2.supervisor import V2ReadSupervisor
from app.v2.tools import CatalogSnapshot, V2ReadTools
from app.v2.turn_service import V2TurnService
from tests.test_evaluation_v3_executor import _context_and_case

CORPUS_ID = f"cor_{'a' * 60}"
INDEX_ID = f"idx_{'b' * 60}"


class _Payload(BaseModel):
    value: str


class _RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> StructuredModelResult[Any]:
        self.calls.append(kwargs)
        metadata = _metadata(
            len(self.calls),
            stage=kwargs["stage"],
            agent_id=kwargs["agent_id"],
        )
        schema = kwargs["schema"]
        if kwargs["stage"] == "knowledge_query_plan":
            payload = json.loads(kwargs["input_text"])
            value = schema(
                queries=("rewritten topic",),
                source_ids=(payload["authorized_sources"][0]["source_id"],),
            )
        else:
            value = schema(value="ok")
        with collect_model_calls() as calls:
            calls.append(metadata)
        return StructuredModelResult(
            value=value,
            metadata=metadata,
        )


class _EmbeddingRuntime:
    dimensions = 128
    method = "embedding_frozen_v3"
    model = "embedding_frozen_v3"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.0] * self.dimensions for _ in texts]


class _ForbiddenDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, operation: object, access: object) -> object:
        del operation, access
        self.calls += 1
        raise AssertionError("disabled knowledge operation crossed the executor")


class _NoPersistenceExecutor(DurableOperationExecutor):
    def _load_results(self, turn_id: str, access: object) -> dict[str, object]:
        del turn_id, access
        return {}


class _EmptyOperationExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def execute(
        self,
        *,
        operations: tuple[RuntimeOperation, ...],
        **_: object,
    ) -> OperationBatch:
        self.calls.append(tuple(operation.capability for operation in operations))
        return OperationBatch(results=(), dispatched_step_ids=(), reused_step_ids=())

    def checkpoint_draft_repair(self, **_: object) -> None:
        raise AssertionError("an abstaining turn cannot draft or repair")


class _ForbiddenAnswerProducer:
    def __init__(self) -> None:
        self.calls = 0

    async def produce(self, **_: object) -> GroundingResult:
        self.calls += 1
        raise AssertionError("missing required evidence must abstain before drafting")


class _AbstainingAnswerProducer:
    async def produce(self, **_: object) -> GroundingResult:
        return GroundingResult(
            outcome=DialogueOutcome.ABSTAINED,
            answer="No grounded answer is available.",
        )


class _ContinuationProbePlanner:
    def __init__(self) -> None:
        self.continuation_calls = 0

    async def plan(self, message: str, context: PlanningContext) -> PlannedTurn:
        del message, context
        return PlannedTurn(
            plan_id="plan_probe",
            intent="probe",
            template_id="template_probe",
            obligations=(),
            desired_capabilities=(),
            initial_operations=(),
            deferred_capabilities=(),
            candidate_limit=1,
            query="probe",
        )

    def bind_initial_candidates(self, *_: object) -> tuple[RuntimeOperation, ...]:
        return ()

    def continue_plan(self, *_: object) -> tuple[RuntimeOperation, ...]:
        self.continuation_calls += 1
        return ()


def _unused_session_factory() -> Session:
    raise AssertionError("this focused test cannot open a database session")


def _knowledge_snapshot() -> PublishedKnowledgeSnapshot:
    spec = IndexBuildSpec(
        embedding_model="embedding_frozen_v3",
        embedding_dimension=128,
        chunker_version="chunker_v1",
        enrichment_policy_version="enrichment_v1",
    )
    return PublishedKnowledgeSnapshot(
        corpus_version_id=CORPUS_ID,
        corpus_name="evaluation-v3",
        corpus_version="1",
        index_manifest_id=INDEX_ID,
        index_fingerprint=spec.index_fingerprint,
        embedding_model=spec.embedding_model,
        embedding_dimension=spec.embedding_dimension,
        chunker_version=spec.chunker_version,
        enrichment_policy_version=spec.enrichment_policy_version,
        query_embedding_fingerprint=spec.query_embedding_fingerprint,
        retrieval_policy=RetrievalPolicy(
            version="policy_v1",
            dense_weight=1.0,
            lexical_weight=1.0,
            min_dense_relevance=0.1,
            min_lexical_coverage=0.1,
        ),
        published_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def _knowledge_source() -> AuthorizedKnowledgeSource:
    return AuthorizedKnowledgeSource(
        source_id="src_evaluation_v3",
        source_version_id=content_addressed_id("svr", {"source": "evaluation-v3"}),
        title="Evaluation source",
        url="https://example.test/evaluation-v3",
        planning_text="Public topic information",
        keywords=("topic",),
        support_scope=SourceSupportScope.WORK,
        work_identifier="work_evaluation_v3",
        retrieved_at=datetime(2026, 9, 14, tzinfo=UTC),
    )


def _shared_services(
    *,
    planning_model: str = PINNED_GENERATION_MODEL_V3,
) -> V2ServiceGraph:
    catalog = CatalogSnapshot(
        version_id="catalog_frozen_v3",
        observed_at=datetime(2026, 9, 14, tzinfo=UTC),
        source_ids=(1,),
    )
    knowledge = _knowledge_snapshot()
    knowledge_store = cast(Any, object())
    embedding_runtime = _EmbeddingRuntime()
    base_query_planner = ModelRuntimeKnowledgeQueryPlanner(
        cast(ModelRuntime, _RecordingRuntime()),
        model=PINNED_GENERATION_MODEL_V3,
        fallback_on_model_error=False,
    )
    knowledge_service = KnowledgeService(
        HybridKnowledgeRetriever(
            knowledge_store,
            embedding_runtime,
            planner=base_query_planner,
        ),
        store=knowledge_store,
    )
    action_service = cast(Any, object())
    tools = V2ReadTools(
        _unused_session_factory,
        catalog_snapshot=catalog,
        knowledge_service=knowledge_service,
        knowledge_snapshot=knowledge,
        action_service=action_service,
        registry=default_v2_registry,
    )
    planner = BoundedV2Planner(registry=default_v2_registry, runtime_mode="off")
    operation_executor = DurableOperationExecutor(
        _unused_session_factory,
        tools,
        registry=default_v2_registry,
    )
    answer = GroundedAnswerProducer(None, runtime_mode="off", model=None)
    supervisor = V2ReadSupervisor(
        _unused_session_factory,
        planner=planner,
        operation_executor=operation_executor,
        answer_producer=answer,
    )
    durable = DurableReadTurnExecutor(_unused_session_factory, supervisor)
    return V2ServiceGraph(
        session_factory=_unused_session_factory,
        versions=RuntimeDataVersions(
            catalog_version_id=catalog.version_id,
            corpus_version_id=CORPUS_ID,
            index_manifest_id=INDEX_ID,
        ),
        model_snapshot=V2ModelSnapshot(
            runtime_mode="required",
            planning_model=planning_model,
            specialist_model=PINNED_GENERATION_MODEL_V3,
            synthesis_model=PINNED_GENERATION_MODEL_V3,
            embedding_model=knowledge.embedding_model,
            embedding_dimensions=knowledge.embedding_dimension,
            provider_runtime_configured=True,
        ),
        catalog_snapshot=catalog,
        knowledge_snapshot=knowledge,
        knowledge_store=knowledge_store,
        knowledge_service=knowledge_service,
        action_service=action_service,
        read_tools=tools,
        planner=planner,
        operation_executor=operation_executor,
        answer_producer=answer,
        supervisor=supervisor,
        executor=durable,
        turn_service=V2TurnService(durable),
        budget_ledger=cast(Any, object()),
        budget_account_id="evaluation_v3",
    )


def _context(*, resolved_product_ids: tuple[int, ...] = (1,)) -> PlanningContext:
    access = bind_request_authorization(
        AuthorizationContext(
            tenant_id="tenant_evaluation_v3",
            principal_id="principal-evaluation-v3",
            scopes=frozenset({"ecommerce.read"}),
        ),
        ConversationMode.SHOPPER,
    )
    return PlanningContext(
        access=access,
        versions=RuntimeDataVersions(
            catalog_version_id="catalog_frozen_v3",
            corpus_version_id=CORPUS_ID,
            index_manifest_id=INDEX_ID,
        ),
        resolved_product_ids=resolved_product_ids,
    )


def _budget_context() -> ProviderBudgetContext:
    return ProviderBudgetContext(
        ledger=cast(Any, object()),
        scope_id="evaluation_v3_knowledge_plan",
        purpose="benchmark",
    )


def _metadata(
    index: int,
    *,
    stage: str = "answer.draft",
    agent_id: str = "grounding",
    response_id: str | None = None,
) -> ModelCallMetadata:
    return ModelCallMetadata(
        call_id=f"mcall_{index:032x}",
        stage=stage,
        agent_id=agent_id,
        model=PINNED_GENERATION_MODEL_V3,
        response_id=response_id,
        status="success",
        duration_ms=1,
        input_tokens=3,
        cached_input_tokens=1,
        output_tokens=2,
        reasoning_tokens=1,
        total_tokens=5,
        attempts=1,
    )


def test_package7_policy_table_is_exact_and_rejects_arbitrary_combinations() -> None:
    assert [policy.variant_id for policy in PACKAGE7_VARIANT_POLICIES] == [
        "sa_shared_tools_rag",
        "ma_fixed_rag",
        "ma_adaptive_rag",
        "ma_adaptive_no_rag",
    ]
    assert [
        (
            policy.agent_topology,
            policy.planning_mode,
            policy.max_continuations,
            policy.rag_enabled,
        )
        for policy in PACKAGE7_VARIANT_POLICIES
    ] == [
        (AgentTopologyV3.SINGLE, PlanningModeV3.SHARED, 0, True),
        (AgentTopologyV3.MULTI, PlanningModeV3.FIXED, 0, True),
        (AgentTopologyV3.MULTI, PlanningModeV3.ADAPTIVE, 1, True),
        (AgentTopologyV3.MULTI, PlanningModeV3.ADAPTIVE, 1, False),
    ]
    with pytest.raises(
        EvaluationV3RuntimeConfigurationError,
        match="package7_variant_policy_invalid",
    ):
        EvaluationV3VariantPolicy(
            variant_id="ma_fixed_rag",
            agent_topology=AgentTopologyV3.MULTI,
            planning_mode=PlanningModeV3.FIXED,
            max_continuations=1,
            rag_enabled=True,
        )
    with pytest.raises(
        EvaluationV3RuntimeConfigurationError,
        match="package7_variant_policy_invalid",
    ):
        EvaluationV3VariantPolicy(
            variant_id="ma_fixed_rag",
            agent_topology="multi",  # type: ignore[arg-type]
            planning_mode=PlanningModeV3.FIXED,
            max_continuations=0,
            rag_enabled=True,
        )


def test_composer_rejects_shared_model_snapshot_drift() -> None:
    with pytest.raises(
        EvaluationV3RuntimeConfigurationError,
        match="package7_shared_model_snapshot_drift",
    ):
        EvaluationV3VariantComposer(
            _shared_services(planning_model="different-model"),
            model_runtime=cast(ModelRuntime, _RecordingRuntime()),
        )


def test_composer_maps_all_topologies_over_one_shared_resource_graph() -> None:
    shared = _shared_services()
    composer = EvaluationV3VariantComposer(
        shared,
        model_runtime=cast(ModelRuntime, _RecordingRuntime()),
    )
    runtimes = composer.compose_all()
    single, fixed, adaptive, no_rag = runtimes
    shared_retriever = shared.knowledge_service.retriever

    assert all(runtime.shared_services is shared for runtime in runtimes)
    assert all(
        runtime.read_tools is runtime.operation_executor.dispatcher
        for runtime in runtimes
    )
    assert all(runtime.read_tools is not shared.read_tools for runtime in runtimes)
    assert all(
        runtime.read_tools.registry is shared.read_tools.registry
        for runtime in runtimes
    )
    assert all(
        runtime.read_tools.catalog_snapshot is shared.catalog_snapshot
        and runtime.read_tools.knowledge_snapshot is shared.knowledge_snapshot
        and runtime.read_tools.action_service is shared.action_service
        for runtime in runtimes
    )
    assert all(
        runtime.knowledge_service.store is shared.knowledge_store
        and runtime.knowledge_service.retriever.store is shared.knowledge_store
        and runtime.knowledge_service.retriever.embedder is shared_retriever.embedder
        for runtime in runtimes
    )
    assert all(
        runtime.knowledge_service is not shared.knowledge_service
        for runtime in runtimes
    )
    assert all(
        runtime.knowledge_service.retriever.planner is not shared_retriever.planner
        for runtime in runtimes
    )
    assert len(
        {id(runtime.knowledge_service.retriever.planner) for runtime in runtimes}
    ) == len(runtimes)
    assert all(
        isinstance(
            runtime.knowledge_service.retriever.planner,
            ModelRuntimeKnowledgeQueryPlanner,
        )
        and runtime.knowledge_service.retriever.planner.runtime is runtime.model_runtime
        and runtime.knowledge_service.retriever.planner.model
        == PINNED_GENERATION_MODEL_V3
        and runtime.knowledge_service.retriever.planner.fallback_on_model_error is False
        for runtime in runtimes
    )
    assert single.operation_executor.expert_reasoner is None
    assert single.planner.runtime_mode == "required"
    assert single.planner.rag_enabled is True
    assert single.supervisor.continuation_enabled is False
    assert single.executor.model_call_observer is not None
    assert single.executor.allowed_budget_purposes == frozenset({"warmup", "benchmark"})

    assert fixed.planner.runtime_mode == "off"
    assert fixed.operation_executor.expert_reasoner is not None
    assert fixed.supervisor.continuation_enabled is False
    assert fixed.supervisor.knowledge_resolver is not None

    assert adaptive.planner.runtime_mode == "required"
    assert adaptive.operation_executor.expert_reasoner is not None
    assert adaptive.supervisor.continuation_enabled is True
    assert adaptive.operation_executor.rag_enabled is True

    assert no_rag.planner.runtime_mode == "required"
    assert no_rag.operation_executor.expert_reasoner is not None
    assert no_rag.supervisor.continuation_enabled is True
    assert no_rag.planner.rag_enabled is False
    assert no_rag.operation_executor.rag_enabled is False
    assert no_rag.supervisor.knowledge_resolver is None


def test_concrete_executor_factory_composes_all_four_variants_fresh() -> None:
    shared = _shared_services()
    factory = EvaluationV3ObservationExecutorFactory(
        shared,
        model_runtime=cast(ModelRuntime, _RecordingRuntime()),
    )

    for policy in PACKAGE7_VARIANT_POLICIES:
        variant_id = policy.variant_id
        context, case = _context_and_case(
            variant_id=variant_id,
            run_id=f"run_factory_{variant_id}",
        )
        first = factory(context=context, case=case)
        second = factory(context=context, case=case)

        assert isinstance(first, EvaluationV3ObservationExecutor)
        assert first.runtime is not second.runtime
        assert first.runtime.shared_services is shared
        assert first.runtime.policy == policy


@pytest.mark.asyncio
async def test_single_controller_maps_planning_and_answer_and_forbids_specialist() -> (
    None
):
    delegate = _RecordingRuntime()
    runtime = EvaluationV3VariantComposer(
        _shared_services(),
        model_runtime=cast(ModelRuntime, delegate),
    ).compose("sa_shared_tools_rag")

    for stage, agent_id in (
        ("v2_planning", "supervisor"),
        ("answer.draft", "grounding"),
    ):
        await runtime.model_runtime.generate_structured(
            stage=stage,
            agent_id=agent_id,
            model=PINNED_GENERATION_MODEL_V3,
            instructions="bounded",
            input_text="bounded",
            schema=_Payload,
        )
    assert [call["agent_id"] for call in delegate.calls] == [
        CONTROLLER_ACTOR_ID_V3,
        CONTROLLER_ACTOR_ID_V3,
    ]
    with pytest.raises(
        EvaluationV3RuntimeConfigurationError,
        match="single_controller_specialist_call_forbidden",
    ):
        await runtime.model_runtime.generate_structured(
            stage="v2_expert_reasoning",
            agent_id="catalog_expert",
            model=PINNED_GENERATION_MODEL_V3,
            instructions="bounded",
            input_text="bounded",
            schema=_Payload,
        )
    assert len(delegate.calls) == 2


@pytest.mark.asyncio
async def test_single_controller_wired_rag_planner_is_captured_once() -> None:
    delegate = _RecordingRuntime()
    runtime = EvaluationV3VariantComposer(
        _shared_services(),
        model_runtime=cast(ModelRuntime, delegate),
    ).compose("sa_shared_tools_rag")
    planner = runtime.knowledge_service.retriever.planner

    assert isinstance(planner, ModelRuntimeKnowledgeQueryPlanner)
    with provider_budget_scope(_budget_context()), runtime.model_calls.capture():
        plan = await planner.plan("topic", (_knowledge_source(),))

    assert plan.planner == "model"
    assert len(delegate.calls) == 1
    assert delegate.calls[0]["stage"] == "knowledge_query_plan"
    assert delegate.calls[0]["agent_id"] == CONTROLLER_ACTOR_ID_V3
    evidence = runtime.model_calls.snapshot()
    assert len(evidence) == 1
    assert evidence[0].stage == "knowledge_query_plan"
    assert evidence[0].actor_id == CONTROLLER_ACTOR_ID_V3


@pytest.mark.asyncio
async def test_fixed_plan_declares_candidate_reads_before_evidence() -> None:
    runtime = EvaluationV3VariantComposer(
        _shared_services(),
        model_runtime=cast(ModelRuntime, _RecordingRuntime()),
    ).compose("ma_fixed_rag")
    planned = await runtime.planner.plan(
        "Tìm sách lịch sử, so sánh và cho biết chủ đề",
        _context(resolved_product_ids=()),
    )
    declared = set(planned.desired_capabilities)
    bound = runtime.planner.bind_initial_candidates(
        planned,
        (1, 2),
        _context(resolved_product_ids=()),
        planned.initial_operations,
    )
    selected = {
        operation.capability for operation in (*planned.initial_operations, *bound)
    }
    assert selected <= declared
    assert runtime.supervisor.continuation_enabled is False


@pytest.mark.asyncio
async def test_supervisor_default_continuation_stays_enabled_but_can_be_disabled() -> (
    None
):
    disabled_planner = _ContinuationProbePlanner()
    disabled = V2ReadSupervisor(
        _unused_session_factory,
        planner=cast(Any, disabled_planner),
        operation_executor=cast(Any, _EmptyOperationExecutor()),
        answer_producer=_AbstainingAnswerProducer(),
        continuation_enabled=False,
    )
    await disabled.run_claimed(
        conversation_id="conversation_probe",
        turn_id="turn_probe_disabled",
        lease_owner="worker_probe",
        message="probe",
        context=_context(),
        deadline_monotonic=10**12,
    )
    assert disabled_planner.continuation_calls == 0

    default_planner = _ContinuationProbePlanner()
    default = V2ReadSupervisor(
        _unused_session_factory,
        planner=cast(Any, default_planner),
        operation_executor=cast(Any, _EmptyOperationExecutor()),
        answer_producer=_AbstainingAnswerProducer(),
    )
    await default.run_claimed(
        conversation_id="conversation_probe",
        turn_id="turn_probe_default",
        lease_owner="worker_probe",
        message="probe",
        context=_context(),
        deadline_monotonic=10**12,
    )
    assert default_planner.continuation_calls == 1


@pytest.mark.asyncio
async def test_no_rag_compiler_retains_obligation_and_supervisor_abstains() -> None:
    planner = BoundedV2Planner(runtime_mode="off", rag_enabled=False)
    context = _context()
    planned = await planner.plan("Sách này nói về chủ đề gì?", context)

    assert any(
        obligation.kind.value == "knowledge" for obligation in planned.obligations
    )
    assert "knowledge.retrieve" not in planned.desired_capabilities
    assert all(
        operation.capability != "knowledge.retrieve"
        for operation in planned.initial_operations
    )

    executor = _EmptyOperationExecutor()
    producer = _ForbiddenAnswerProducer()
    computation = await V2ReadSupervisor(
        _unused_session_factory,
        planner=planner,
        operation_executor=cast(Any, executor),
        answer_producer=producer,
        continuation_enabled=True,
    ).run_claimed(
        conversation_id="conversation_no_rag",
        turn_id="turn_no_rag",
        lease_owner="worker_no_rag",
        message="Sách này nói về chủ đề gì?",
        context=context,
        deadline_monotonic=10**12,
    )
    assert computation.result.outcome is DialogueOutcome.ABSTAINED
    assert computation.knowledge_retrievals == 0
    assert computation.result.citations == ()
    assert producer.calls == 0
    assert executor.calls == [()]


@pytest.mark.asyncio
async def test_no_rag_executor_and_model_runtime_reject_knowledge_before_dispatch() -> (
    None
):
    context = _context()
    knowledge_plan = await BoundedV2Planner(runtime_mode="off").plan(
        "Sách này nói về chủ đề gì?",
        context,
    )
    knowledge_operation = knowledge_plan.initial_operations[0]
    dispatcher = _ForbiddenDispatcher()
    executor = _NoPersistenceExecutor(
        _unused_session_factory,
        cast(Any, dispatcher),
        rag_enabled=False,
    )
    with pytest.raises(
        DurableExecutionError,
        match="knowledge_capability_disabled",
    ):
        await executor.execute(
            turn_id="turn_no_rag_executor",
            lease_owner="worker_no_rag",
            access=context.access,
            operations=(knowledge_operation,),
            plan_revision=0,
        )
    assert dispatcher.calls == 0

    delegate = _RecordingRuntime()
    shared = _shared_services()
    no_rag = EvaluationV3VariantComposer(
        shared,
        model_runtime=cast(ModelRuntime, delegate),
    ).compose("ma_adaptive_no_rag")
    local_planner = no_rag.knowledge_service.retriever.planner
    assert isinstance(local_planner, ModelRuntimeKnowledgeQueryPlanner)
    assert local_planner.runtime is no_rag.model_runtime
    with pytest.raises(
        DurableExecutionError,
        match="knowledge_capability_disabled",
    ):
        no_rag.operation_executor._validate_operation(  # noqa: SLF001
            knowledge_operation,
            context.access,
        )
    with pytest.raises(
        EvaluationV3RuntimeConfigurationError,
        match="no_rag_knowledge_model_call_forbidden",
    ):
        await no_rag.model_runtime.generate_structured(
            stage="knowledge_query_plan",
            agent_id="knowledge",
            model=PINNED_GENERATION_MODEL_V3,
            instructions="bounded",
            input_text="bounded",
            schema=_Payload,
        )
    assert delegate.calls == []
    embedder = shared.knowledge_service.retriever.embedder
    assert isinstance(embedder, _EmbeddingRuntime)
    assert embedder.calls == 0


def test_finalized_observer_is_sanitized_deduplicated_and_nested() -> None:
    canary = "private-response-payload-canary"
    policy = package7_variant_policy("sa_shared_tools_rag")
    recorder = EvaluationV3ModelCallRecorder(policy)
    metadata = _metadata(50, response_id=canary)

    with recorder.capture():
        with collect_model_calls() as inner:
            inner.append(metadata)
            mark_model_call_fallback(metadata, "checked_evidence_answer")
    with recorder.capture():
        with collect_model_calls() as replay:
            replay.append(metadata)

    evidence = recorder.snapshot()
    assert len(evidence) == 1
    assert evidence[0].actor_id == CONTROLLER_ACTOR_ID_V3
    assert evidence[0].fallback_used is True
    assert evidence[0].fallback_reason == "checked_evidence_answer"
    assert evidence[0].requested_model == PINNED_GENERATION_MODEL_V3
    assert evidence[0].resolved_model == PINNED_GENERATION_MODEL_V3
    assert set(evidence[0].model_dump()) == {
        "call_id",
        "stage",
        "actor_id",
        "requested_model",
        "resolved_model",
        "status",
        "attempts",
        "fallback_used",
        "fallback_reason",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
    assert canary not in repr(evidence)


@pytest.mark.asyncio
async def test_model_call_capture_is_context_isolated() -> None:
    first = EvaluationV3ModelCallRecorder(package7_variant_policy("ma_adaptive_rag"))
    second = EvaluationV3ModelCallRecorder(package7_variant_policy("ma_adaptive_rag"))

    async def capture(
        recorder: EvaluationV3ModelCallRecorder,
        metadata: ModelCallMetadata,
    ) -> None:
        with recorder.capture():
            await asyncio.sleep(0)
            with collect_model_calls() as nested:
                nested.append(metadata)

    await asyncio.gather(capture(first, _metadata(71)), capture(second, _metadata(72)))
    assert [call.call_id for call in first.snapshot()] == [f"mcall_{71:032x}"]
    assert [call.call_id for call in second.snapshot()] == [f"mcall_{72:032x}"]
