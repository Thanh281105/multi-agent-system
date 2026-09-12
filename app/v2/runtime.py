"""Lazy production composition for the durable v2 runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import TypeAlias, cast

from sqlalchemy.orm import Session, sessionmaker

from app.contracts import AuthorizationContext
from app.core.config import Settings
from app.knowledge.postgres import PostgresKnowledgeStore
from app.knowledge.retrieval import (
    HybridKnowledgeRetriever,
    KnowledgeRetrievalStore,
    ModelRuntimeKnowledgeQueryPlanner,
)
from app.knowledge.runtime import build_knowledge_embedder
from app.knowledge.service import KnowledgeService
from app.knowledge.v2_contracts import PublishedKnowledgeSnapshot
from app.shared import EmbeddingRuntime, ModelRuntime, ModelRuntimeMode
from app.shared.budget import (
    PricingManifest,
    SQLProviderBudgetLedger,
    default_pricing_manifest_path,
)
from app.v2.actions import V2ActionService
from app.v2.answers import GroundedAnswerProducer
from app.v2.authorization import ResourceAuthorization, bind_request_authorization
from app.v2.contracts import ConversationMode
from app.v2.execution import (
    DurableOperationExecutor,
    DurableReadTurnExecutor,
    ModelRuntimeExpertReasoner,
)
from app.v2.history import V2HistoryService
from app.v2.planning import BoundedV2Planner, PlanningContext, RuntimeDataVersions
from app.v2.supervisor import (
    GroundedAnswerProducer as SupervisorAnswerProducer,
)
from app.v2.supervisor import V2ReadSupervisor
from app.v2.tools import CatalogSnapshot, V2ReadTools, load_catalog_snapshot
from app.v2.turn_service import V2TurnService

SessionFactory: TypeAlias = Callable[[], Session]
SessionFactoryBuilder: TypeAlias = Callable[[str], SessionFactory]
CatalogSnapshotLoader: TypeAlias = Callable[[SessionFactory], CatalogSnapshot]
KnowledgeStoreFactory: TypeAlias = Callable[[SessionFactory], KnowledgeRetrievalStore]
EmbeddingRuntimeFactory: TypeAlias = Callable[[Settings], EmbeddingRuntime]
PricingManifestLoader: TypeAlias = Callable[[], PricingManifest]


class V2RuntimeConfigurationError(RuntimeError):
    """A required server-owned v2 runtime setting is absent or inconsistent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class V2ModelSnapshot:
    """Server-selected model configuration pinned into one service graph."""

    runtime_mode: ModelRuntimeMode
    planning_model: str
    specialist_model: str
    synthesis_model: str
    embedding_model: str
    embedding_dimensions: int
    provider_runtime_configured: bool


@dataclass(frozen=True, slots=True)
class V2ServiceGraph:
    """Long-lived v2 services built from one immutable snapshot selection."""

    session_factory: SessionFactory
    versions: RuntimeDataVersions
    model_snapshot: V2ModelSnapshot
    catalog_snapshot: CatalogSnapshot
    knowledge_snapshot: PublishedKnowledgeSnapshot
    knowledge_store: KnowledgeRetrievalStore
    knowledge_service: KnowledgeService
    action_service: V2ActionService
    read_tools: V2ReadTools
    planner: BoundedV2Planner
    operation_executor: DurableOperationExecutor
    answer_producer: GroundedAnswerProducer
    supervisor: V2ReadSupervisor
    executor: DurableReadTurnExecutor
    turn_service: V2TurnService
    budget_ledger: SQLProviderBudgetLedger
    budget_account_id: str

    @contextmanager
    def history(self) -> Iterator[V2HistoryService]:
        """Open one short-lived history unit of work for a future route/service."""

        with self.session_factory() as session:
            yield V2HistoryService(session)


@dataclass(frozen=True, slots=True)
class ResolvedV2Runtime:
    """Request-local authority combined with the pinned shared service graph."""

    access: ResourceAuthorization
    planning_context: PlanningContext
    services: V2ServiceGraph

    @property
    def versions(self) -> RuntimeDataVersions:
        return self.services.versions

    @property
    def model_snapshot(self) -> V2ModelSnapshot:
        return self.services.model_snapshot

    @property
    def executor(self) -> DurableReadTurnExecutor:
        return self.services.executor


class V2RuntimeFactory:
    """Resolve server authority, then lazily assemble one deterministic v2 graph."""

    def __init__(
        self,
        config: Settings,
        *,
        session_factory: SessionFactory | None = None,
        session_factory_builder: SessionFactoryBuilder | None = None,
        model_runtime: ModelRuntime | None = None,
        embedding_runtime: EmbeddingRuntime | None = None,
        embedding_runtime_factory: EmbeddingRuntimeFactory | None = None,
        catalog_snapshot_loader: CatalogSnapshotLoader | None = None,
        knowledge_store_factory: KnowledgeStoreFactory | None = None,
        pricing_manifest_loader: PricingManifestLoader | None = None,
    ) -> None:
        self._config = config
        self._injected_session_factory = session_factory
        self._session_factory_builder = session_factory_builder
        self._model_runtime = model_runtime
        self._injected_embedding_runtime = embedding_runtime
        self._embedding_runtime_factory = embedding_runtime_factory
        self._catalog_snapshot_loader = catalog_snapshot_loader
        self._knowledge_store_factory = knowledge_store_factory
        self._pricing_manifest_loader = pricing_manifest_loader
        self._services: V2ServiceGraph | None = None
        self._lock = Lock()

    @property
    def initialized(self) -> bool:
        """Whether a request has successfully resolved the shared v2 graph."""

        return self._services is not None

    def resolve_for_request(
        self,
        authorization: AuthorizationContext,
        *,
        mode: ConversationMode,
    ) -> ResolvedV2Runtime:
        """Bind trusted gateway identity without accepting client authority fields."""

        access = bind_request_authorization(authorization, ConversationMode(mode))
        services = self._resolve_services()
        context = PlanningContext(access=access, versions=services.versions)
        return ResolvedV2Runtime(
            access=access,
            planning_context=context,
            services=services,
        )

    def _resolve_services(self) -> V2ServiceGraph:
        services = self._services
        if services is not None:
            return services
        with self._lock:
            services = self._services
            if services is None:
                services = self._build_services()
                self._services = services
            return services

    def _build_services(self) -> V2ServiceGraph:
        corpus_version_id = self._config.v2_corpus_version_id
        if corpus_version_id is None:
            raise V2RuntimeConfigurationError(
                "v2_corpus_version_not_configured",
                "V2_CORPUS_VERSION_ID is required before resolving the v2 runtime",
            )
        model_names = {
            "OPENAI_PLANNING_MODEL": self._config.openai_planning_model,
            "OPENAI_SPECIALIST_MODEL": self._config.openai_specialist_model,
            "OPENAI_SYNTHESIS_MODEL": self._config.openai_synthesis_model,
        }
        blank_model = next(
            (name for name, value in model_names.items() if not value.strip()),
            None,
        )
        if blank_model is not None:
            raise V2RuntimeConfigurationError(
                "v2_model_snapshot_invalid",
                f"{blank_model} must not be blank for the v2 runtime",
            )
        if (
            self._config.model_runtime_mode == "required"
            and self._model_runtime is None
        ):
            raise V2RuntimeConfigurationError(
                "v2_model_runtime_not_configured",
                "required v2 model mode needs the shared gateway model runtime",
            )

        session_factory = self._injected_session_factory
        if session_factory is None:
            builder = self._session_factory_builder or _default_session_factory_builder
            session_factory = builder(self._config.database_url)

        catalog_loader = self._catalog_snapshot_loader or load_catalog_snapshot
        catalog_snapshot = catalog_loader(session_factory)
        store_factory = (
            self._knowledge_store_factory or _default_knowledge_store_factory
        )
        knowledge_store = store_factory(session_factory)
        knowledge_snapshot = knowledge_store.resolve_published_snapshot(
            corpus_version_id,
            index_manifest_id=self._config.v2_index_manifest_id,
        )
        if knowledge_snapshot.corpus_version_id != corpus_version_id:
            raise V2RuntimeConfigurationError(
                "v2_corpus_snapshot_mismatch",
                "resolved corpus snapshot does not match V2_CORPUS_VERSION_ID",
            )
        configured_index = self._config.v2_index_manifest_id
        if (
            configured_index is not None
            and knowledge_snapshot.index_manifest_id != configured_index
        ):
            raise V2RuntimeConfigurationError(
                "v2_index_snapshot_mismatch",
                "resolved index snapshot does not match V2_INDEX_MANIFEST_ID",
            )

        embedding_runtime = self._injected_embedding_runtime
        if embedding_runtime is None:
            embedding_factory = (
                self._embedding_runtime_factory or build_knowledge_embedder
            )
            embedding_runtime = embedding_factory(self._config)
        runtime_model = getattr(
            embedding_runtime,
            "model",
            getattr(embedding_runtime, "method", "unknown"),
        )
        if (
            str(runtime_model) != knowledge_snapshot.embedding_model
            or embedding_runtime.dimensions != knowledge_snapshot.embedding_dimension
        ):
            raise V2RuntimeConfigurationError(
                "v2_embedding_snapshot_mismatch",
                "v2 embedding runtime does not match the published index snapshot",
            )

        knowledge_planner = None
        if self._model_runtime is not None and self._config.model_runtime_mode != "off":
            knowledge_planner = ModelRuntimeKnowledgeQueryPlanner(
                self._model_runtime,
                model=self._config.openai_planning_model,
                fallback_on_model_error=self._config.model_runtime_mode != "required",
            )
        retriever = HybridKnowledgeRetriever(
            knowledge_store,
            embedding_runtime,
            planner=knowledge_planner,
        )
        knowledge_service = KnowledgeService(retriever, store=knowledge_store)
        action_service = V2ActionService(
            session_factory,
            catalog_version_id=catalog_snapshot.version_id,
        )
        read_tools = V2ReadTools(
            session_factory,
            catalog_snapshot=catalog_snapshot,
            knowledge_service=knowledge_service,
            knowledge_snapshot=knowledge_snapshot,
            action_service=action_service,
        )
        planner = BoundedV2Planner(
            model_runtime=self._model_runtime,
            runtime_mode=self._config.model_runtime_mode,
            model=self._config.openai_planning_model,
            reasoning_effort=self._config.openai_reasoning_effort,
        )
        expert_model = (
            self._config.openai_specialist_model
            if self._model_runtime is not None
            else None
        )
        expert_reasoner = ModelRuntimeExpertReasoner(
            self._model_runtime,
            runtime_mode=self._config.model_runtime_mode,
            model=expert_model,
            reasoning_effort=self._config.openai_reasoning_effort,
        )
        operation_executor = DurableOperationExecutor(
            session_factory,
            read_tools,
            expert_reasoner=expert_reasoner,
        )
        answer_model = (
            self._config.openai_synthesis_model
            if self._model_runtime is not None
            else None
        )
        answer_producer = GroundedAnswerProducer(
            self._model_runtime,
            runtime_mode=self._config.model_runtime_mode,
            model=answer_model,
            reasoning_effort=self._config.openai_reasoning_effort,
            draft_max_output_tokens=self._config.openai_max_output_tokens,
            verifier_max_output_tokens=self._config.openai_max_output_tokens,
        )
        supervisor = V2ReadSupervisor(
            session_factory,
            planner=planner,
            operation_executor=operation_executor,
            answer_producer=cast(SupervisorAnswerProducer, answer_producer),
            knowledge_resolver=read_tools.reopen_knowledge,
            action_service=action_service,
        )
        pricing_loader = (
            self._pricing_manifest_loader or _default_pricing_manifest_loader
        )
        budget_ledger = SQLProviderBudgetLedger(
            session_factory,
            pricing_loader(),
        )
        executor = DurableReadTurnExecutor(
            session_factory,
            supervisor,
            budget_ledger=budget_ledger,
        )
        turn_service = V2TurnService(executor)
        model_snapshot = V2ModelSnapshot(
            runtime_mode=self._config.model_runtime_mode,
            planning_model=self._config.openai_planning_model,
            specialist_model=self._config.openai_specialist_model,
            synthesis_model=self._config.openai_synthesis_model,
            embedding_model=str(runtime_model),
            embedding_dimensions=embedding_runtime.dimensions,
            provider_runtime_configured=self._model_runtime is not None,
        )
        versions = RuntimeDataVersions(
            catalog_version_id=catalog_snapshot.version_id,
            corpus_version_id=knowledge_snapshot.corpus_version_id,
            index_manifest_id=knowledge_snapshot.index_manifest_id,
        )
        return V2ServiceGraph(
            session_factory=session_factory,
            versions=versions,
            model_snapshot=model_snapshot,
            catalog_snapshot=catalog_snapshot,
            knowledge_snapshot=knowledge_snapshot,
            knowledge_store=knowledge_store,
            knowledge_service=knowledge_service,
            action_service=action_service,
            read_tools=read_tools,
            planner=planner,
            operation_executor=operation_executor,
            answer_producer=answer_producer,
            supervisor=supervisor,
            executor=executor,
            turn_service=turn_service,
            budget_ledger=budget_ledger,
            budget_account_id=self._config.v2_budget_account_id,
        )


def build_v2_runtime_factory(
    config: Settings,
    *,
    session_factory: SessionFactory | None = None,
    model_runtime: ModelRuntime | None = None,
    embedding_runtime: EmbeddingRuntime | None = None,
) -> V2RuntimeFactory:
    """Build the lazy v2 composition boundary without opening any connection."""

    return V2RuntimeFactory(
        config,
        session_factory=session_factory,
        model_runtime=model_runtime,
        embedding_runtime=embedding_runtime,
    )


def _default_session_factory_builder(database_url: str) -> SessionFactory:
    # Importing app.db.session creates its legacy module-level engine, so keep the
    # import behind first v2 use as well as the configured engine construction.
    from app.db.session import create_database_engine

    engine = create_database_engine(database_url)
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def _default_knowledge_store_factory(
    session_factory: SessionFactory,
) -> KnowledgeRetrievalStore:
    return PostgresKnowledgeStore(session_factory)


def _default_pricing_manifest_loader() -> PricingManifest:
    return PricingManifest.load(default_pricing_manifest_path())


__all__ = [
    "ResolvedV2Runtime",
    "V2ModelSnapshot",
    "V2RuntimeConfigurationError",
    "V2RuntimeFactory",
    "V2ServiceGraph",
    "build_v2_runtime_factory",
]
