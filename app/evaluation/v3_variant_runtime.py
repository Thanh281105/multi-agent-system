"""Truthful Package 7 variant composition over the durable v2 primitives."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.v3_models import (
    PACKAGE7_VARIANT_ORDER,
    AgentTopologyV3,
    EvaluationVariantV3,
    PlanningModeV3,
    VariantIdV3,
)
from app.knowledge.retrieval import (
    HybridKnowledgeRetriever,
    ModelRuntimeKnowledgeQueryPlanner,
)
from app.knowledge.service import KnowledgeService
from app.shared import (
    ModelCallMetadata,
    ModelRuntime,
    ModelRuntimeMode,
    ReasoningEffort,
    StructuredModelResult,
    collect_model_calls,
)
from app.v2.answers import GroundedAnswerProducer
from app.v2.execution import (
    DurableOperationExecutor,
    DurableReadTurnExecutor,
    ModelRuntimeExpertReasoner,
)
from app.v2.planning import BoundedV2Planner
from app.v2.runtime import V2ServiceGraph
from app.v2.supervisor import (
    GroundedAnswerProducer as SupervisorAnswerProducer,
)
from app.v2.supervisor import V2ReadSupervisor
from app.v2.tools import V2ReadTools
from app.v2.turn_service import V2TurnService

StructuredT = TypeVar("StructuredT", bound=BaseModel)

PINNED_GENERATION_MODEL_V3: Literal["gpt-5.4-mini-2026-03-17"] = (
    "gpt-5.4-mini-2026-03-17"
)
PINNED_REASONING_EFFORT_V3: ReasoningEffort = "low"
PINNED_MAX_OUTPUT_TOKENS_V3 = 1_200
CONTROLLER_ACTOR_ID_V3 = "controller"

_KNOWLEDGE_MODEL_STAGES = frozenset({"knowledge_query_plan"})

_PolicyBehavior = tuple[
    AgentTopologyV3,
    PlanningModeV3,
    Literal[0, 1],
    bool,
]

_EXPECTED_POLICY: dict[str, _PolicyBehavior] = {
    "sa_shared_tools_rag": (
        AgentTopologyV3.SINGLE,
        PlanningModeV3.SHARED,
        0,
        True,
    ),
    "ma_fixed_rag": (
        AgentTopologyV3.MULTI,
        PlanningModeV3.FIXED,
        0,
        True,
    ),
    "ma_adaptive_rag": (
        AgentTopologyV3.MULTI,
        PlanningModeV3.ADAPTIVE,
        1,
        True,
    ),
    "ma_adaptive_no_rag": (
        AgentTopologyV3.MULTI,
        PlanningModeV3.ADAPTIVE,
        1,
        False,
    ),
}


class EvaluationV3RuntimeConfigurationError(ValueError):
    """A Package 7 runtime binding differs from the frozen experiment contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvaluationV3VariantPolicy:
    """One of the four complete Package 7 policies; partial combinations are invalid."""

    variant_id: VariantIdV3
    agent_topology: AgentTopologyV3
    planning_mode: PlanningModeV3
    max_continuations: Literal[0, 1]
    rag_enabled: bool

    def __post_init__(self) -> None:
        expected = _EXPECTED_POLICY.get(self.variant_id)
        types_valid = bool(
            isinstance(self.agent_topology, AgentTopologyV3)
            and isinstance(self.planning_mode, PlanningModeV3)
            and type(self.max_continuations) is int
            and type(self.rag_enabled) is bool
        )
        actual = (
            self.agent_topology,
            self.planning_mode,
            self.max_continuations,
            self.rag_enabled,
        )
        if not types_valid or expected is None or actual != expected:
            raise EvaluationV3RuntimeConfigurationError(
                "package7_variant_policy_invalid"
            )

    @property
    def specialist_reasoning_enabled(self) -> bool:
        return self.agent_topology is AgentTopologyV3.MULTI

    @property
    def continuation_enabled(self) -> bool:
        return self.max_continuations == 1

    @property
    def planner_runtime_mode(self) -> ModelRuntimeMode:
        if self.planning_mode is PlanningModeV3.FIXED:
            return "off"
        return "required"

    def actor_id(self, stage: str, supplied_agent_id: str) -> str:
        if self.agent_topology is AgentTopologyV3.SINGLE:
            return CONTROLLER_ACTOR_ID_V3
        if stage == "v2_planning" or stage.startswith("answer."):
            return CONTROLLER_ACTOR_ID_V3
        return supplied_agent_id


def package7_variant_policy(
    variant: EvaluationVariantV3 | VariantIdV3,
) -> EvaluationV3VariantPolicy:
    """Resolve only a frozen protocol variant or one of its exact identifiers."""

    if isinstance(variant, EvaluationVariantV3):
        if (
            variant.generation_binding.model != PINNED_GENERATION_MODEL_V3
            or variant.generation_binding.reasoning_effort != PINNED_REASONING_EFFORT_V3
        ):
            raise EvaluationV3RuntimeConfigurationError(
                "package7_generation_binding_drift"
            )
        variant_id = variant.variant_id
    else:
        variant_id = variant
    expected = _EXPECTED_POLICY.get(variant_id)
    if expected is None:
        raise EvaluationV3RuntimeConfigurationError("package7_variant_unknown")
    topology, planning, continuations, rag_enabled = expected
    return EvaluationV3VariantPolicy(
        variant_id=variant_id,
        agent_topology=topology,
        planning_mode=planning,
        max_continuations=continuations,
        rag_enabled=rag_enabled,
    )


PACKAGE7_VARIANT_POLICIES: tuple[EvaluationV3VariantPolicy, ...] = tuple(
    package7_variant_policy(variant_id) for variant_id in PACKAGE7_VARIANT_ORDER
)


class EvaluationV3LogicalModelRuntime:
    """Pin the generation snapshot and expose truthful logical actor identities."""

    def __init__(
        self,
        runtime: ModelRuntime,
        policy: EvaluationV3VariantPolicy,
    ) -> None:
        self.runtime = runtime
        self.policy = policy

    async def generate_structured(
        self,
        *,
        stage: str,
        agent_id: str,
        model: str,
        instructions: str,
        input_text: str,
        schema: type[StructuredT],
        max_output_tokens: int | None = None,
        reasoning_effort: ReasoningEffort = "low",
    ) -> StructuredModelResult[StructuredT]:
        if model != PINNED_GENERATION_MODEL_V3:
            raise EvaluationV3RuntimeConfigurationError(
                "package7_generation_model_drift"
            )
        if reasoning_effort != PINNED_REASONING_EFFORT_V3:
            raise EvaluationV3RuntimeConfigurationError(
                "package7_reasoning_effort_drift"
            )
        if stage == "v2_expert_reasoning" and not (
            self.policy.specialist_reasoning_enabled
        ):
            raise EvaluationV3RuntimeConfigurationError(
                "single_controller_specialist_call_forbidden"
            )
        if stage in _KNOWLEDGE_MODEL_STAGES and not self.policy.rag_enabled:
            raise EvaluationV3RuntimeConfigurationError(
                "no_rag_knowledge_model_call_forbidden"
            )
        return await self.runtime.generate_structured(
            stage=stage,
            agent_id=self.policy.actor_id(stage, agent_id),
            model=model,
            instructions=instructions,
            input_text=input_text,
            schema=schema,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        )


class FinalizedModelCallEvidenceV3(BaseModel):
    """Allowlisted model-call evidence for evaluation scoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(pattern=r"^mcall_[a-f0-9]{32}$")
    stage: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    actor_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    requested_model: Literal["gpt-5.4-mini-2026-03-17"]
    resolved_model: Literal["gpt-5.4-mini-2026-03-17"]
    status: Literal["success", "failed"]
    attempts: int = Field(ge=0, le=10)
    fallback_used: bool
    fallback_reason: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{1,79}$",
        max_length=80,
    )
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class EvaluationV3ModelCallRecorder:
    """Collect finalized nested calls once, with ContextVar isolation from v2."""

    def __init__(self, policy: EvaluationV3VariantPolicy) -> None:
        self.policy = policy
        self._lock = Lock()
        self._seen_call_ids: set[str] = set()
        self._evidence: list[FinalizedModelCallEvidenceV3] = []

    @contextmanager
    def capture(self) -> Iterator[None]:
        calls: list[ModelCallMetadata] = []
        try:
            with collect_model_calls() as calls:
                yield
        finally:
            self.record(calls)

    def record(self, calls: Sequence[ModelCallMetadata]) -> None:
        unique: dict[str, FinalizedModelCallEvidenceV3] = {}
        for metadata in calls:
            unique[metadata.call_id] = self._sanitize(metadata)
        with self._lock:
            for call_id, evidence in unique.items():
                if call_id in self._seen_call_ids:
                    continue
                self._seen_call_ids.add(call_id)
                self._evidence.append(evidence)

    def snapshot(self) -> tuple[FinalizedModelCallEvidenceV3, ...]:
        with self._lock:
            return tuple(self._evidence)

    def _sanitize(self, metadata: ModelCallMetadata) -> FinalizedModelCallEvidenceV3:
        if metadata.model != PINNED_GENERATION_MODEL_V3:
            raise EvaluationV3RuntimeConfigurationError("package7_observed_model_drift")
        if metadata.stage == "v2_expert_reasoning" and not (
            self.policy.specialist_reasoning_enabled
        ):
            raise EvaluationV3RuntimeConfigurationError(
                "single_controller_specialist_call_observed"
            )
        if metadata.stage in _KNOWLEDGE_MODEL_STAGES and not self.policy.rag_enabled:
            raise EvaluationV3RuntimeConfigurationError(
                "no_rag_knowledge_model_call_observed"
            )
        return FinalizedModelCallEvidenceV3(
            call_id=metadata.call_id,
            stage=metadata.stage,
            actor_id=self.policy.actor_id(metadata.stage, metadata.agent_id),
            requested_model=PINNED_GENERATION_MODEL_V3,
            resolved_model=PINNED_GENERATION_MODEL_V3,
            status=metadata.status,
            attempts=metadata.attempts,
            fallback_used=metadata.fallback_used,
            fallback_reason=metadata.fallback_reason,
            input_tokens=metadata.input_tokens,
            cached_input_tokens=metadata.cached_input_tokens,
            output_tokens=metadata.output_tokens,
            reasoning_tokens=metadata.reasoning_tokens,
            total_tokens=metadata.total_tokens,
        )


@dataclass(frozen=True, slots=True)
class EvaluationV3VariantRuntime:
    """Variant-owned orchestration composed over one shared resource graph."""

    policy: EvaluationV3VariantPolicy
    shared_services: V2ServiceGraph
    model_runtime: EvaluationV3LogicalModelRuntime
    model_calls: EvaluationV3ModelCallRecorder
    knowledge_service: KnowledgeService
    read_tools: V2ReadTools
    planner: BoundedV2Planner
    operation_executor: DurableOperationExecutor
    answer_producer: GroundedAnswerProducer
    supervisor: V2ReadSupervisor
    executor: DurableReadTurnExecutor
    turn_service: V2TurnService


class EvaluationV3VariantComposer:
    """Compose the exact four policies without copying their frozen resources."""

    def __init__(
        self,
        shared_services: V2ServiceGraph,
        *,
        model_runtime: ModelRuntime,
    ) -> None:
        _validate_shared_services(shared_services)
        self.shared_services = shared_services
        self.model_runtime = model_runtime

    def compose(
        self,
        variant: EvaluationVariantV3 | VariantIdV3,
    ) -> EvaluationV3VariantRuntime:
        policy = package7_variant_policy(variant)
        runtime = EvaluationV3LogicalModelRuntime(self.model_runtime, policy)
        registry = self.shared_services.read_tools.registry
        shared_retriever = self.shared_services.knowledge_service.retriever
        knowledge_query_planner = ModelRuntimeKnowledgeQueryPlanner(
            runtime,
            model=PINNED_GENERATION_MODEL_V3,
            fallback_on_model_error=False,
        )
        knowledge_retriever = HybridKnowledgeRetriever(
            self.shared_services.knowledge_store,
            shared_retriever.embedder,
            planner=knowledge_query_planner,
        )
        knowledge_service = KnowledgeService(
            knowledge_retriever,
            store=self.shared_services.knowledge_store,
        )
        read_tools = V2ReadTools(
            self.shared_services.session_factory,
            catalog_snapshot=self.shared_services.catalog_snapshot,
            knowledge_service=knowledge_service,
            knowledge_snapshot=self.shared_services.knowledge_snapshot,
            action_service=self.shared_services.action_service,
            registry=registry,
        )
        planner = BoundedV2Planner(
            registry=registry,
            model_runtime=runtime,
            runtime_mode=policy.planner_runtime_mode,
            model=PINNED_GENERATION_MODEL_V3,
            reasoning_effort=PINNED_REASONING_EFFORT_V3,
            rag_enabled=policy.rag_enabled,
        )
        expert_reasoner = (
            ModelRuntimeExpertReasoner(
                runtime,
                runtime_mode="required",
                model=PINNED_GENERATION_MODEL_V3,
                reasoning_effort=PINNED_REASONING_EFFORT_V3,
            )
            if policy.specialist_reasoning_enabled
            else None
        )
        operation_executor = DurableOperationExecutor(
            self.shared_services.session_factory,
            read_tools,
            registry=registry,
            expert_reasoner=expert_reasoner,
            rag_enabled=policy.rag_enabled,
        )
        answer_producer = GroundedAnswerProducer(
            runtime,
            runtime_mode="required",
            model=PINNED_GENERATION_MODEL_V3,
            reasoning_effort=PINNED_REASONING_EFFORT_V3,
            draft_max_output_tokens=PINNED_MAX_OUTPUT_TOKENS_V3,
            verifier_max_output_tokens=PINNED_MAX_OUTPUT_TOKENS_V3,
        )
        supervisor = V2ReadSupervisor(
            self.shared_services.session_factory,
            planner=planner,
            operation_executor=operation_executor,
            answer_producer=cast(SupervisorAnswerProducer, answer_producer),
            knowledge_resolver=(
                read_tools.reopen_knowledge if policy.rag_enabled else None
            ),
            action_service=self.shared_services.action_service,
            continuation_enabled=policy.continuation_enabled,
        )
        model_calls = EvaluationV3ModelCallRecorder(policy)
        executor = DurableReadTurnExecutor(
            self.shared_services.session_factory,
            supervisor,
            budget_ledger=self.shared_services.budget_ledger,
            model_call_observer=model_calls.record,
        )
        return EvaluationV3VariantRuntime(
            policy=policy,
            shared_services=self.shared_services,
            model_runtime=runtime,
            model_calls=model_calls,
            knowledge_service=knowledge_service,
            read_tools=read_tools,
            planner=planner,
            operation_executor=operation_executor,
            answer_producer=answer_producer,
            supervisor=supervisor,
            executor=executor,
            turn_service=V2TurnService(executor),
        )

    def compose_all(
        self,
        variants: Sequence[EvaluationVariantV3] | None = None,
    ) -> tuple[EvaluationV3VariantRuntime, ...]:
        selected: Sequence[EvaluationVariantV3 | VariantIdV3]
        if variants is None:
            selected = PACKAGE7_VARIANT_ORDER
        else:
            variant_ids = tuple(variant.variant_id for variant in variants)
            if variant_ids != PACKAGE7_VARIANT_ORDER:
                raise EvaluationV3RuntimeConfigurationError(
                    "package7_variant_set_or_order_drift"
                )
            selected = variants
        return tuple(self.compose(variant) for variant in selected)


def _validate_shared_services(services: V2ServiceGraph) -> None:
    snapshot = services.model_snapshot
    models = (
        snapshot.planning_model,
        snapshot.specialist_model,
        snapshot.synthesis_model,
    )
    if models != (PINNED_GENERATION_MODEL_V3,) * 3:
        raise EvaluationV3RuntimeConfigurationError(
            "package7_shared_model_snapshot_drift"
        )
    if not snapshot.provider_runtime_configured:
        raise EvaluationV3RuntimeConfigurationError(
            "package7_model_runtime_unavailable"
        )
    if services.catalog_snapshot.version_id != services.versions.catalog_version_id:
        raise EvaluationV3RuntimeConfigurationError("package7_catalog_snapshot_drift")
    knowledge = services.knowledge_snapshot
    if (
        knowledge.corpus_version_id != services.versions.corpus_version_id
        or knowledge.index_manifest_id != services.versions.index_manifest_id
        or knowledge.embedding_model != snapshot.embedding_model
        or knowledge.embedding_dimension != snapshot.embedding_dimensions
    ):
        raise EvaluationV3RuntimeConfigurationError("package7_knowledge_snapshot_drift")
    tools = services.read_tools
    if (
        tools.catalog_snapshot != services.catalog_snapshot
        or tools.knowledge_snapshot != services.knowledge_snapshot
        or tools.knowledge_service is not services.knowledge_service
        or services.knowledge_service.store is not services.knowledge_store
        or services.knowledge_service.retriever.store is not services.knowledge_store
    ):
        raise EvaluationV3RuntimeConfigurationError("package7_read_tool_resource_drift")
