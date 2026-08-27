"""Production-like modular multi-agent orchestration service."""

from __future__ import annotations

from time import perf_counter

from app.agents import AgentDispatcher
from app.contracts import AgentResult, AuthorizationContext
from app.orchestrator.aggregator import ResultAggregator
from app.orchestrator.executor import PlanExecutor
from app.orchestrator.planner import ExecutionPlanner
from app.orchestrator.progress import (
    OrchestrationProgress,
    ProgressCallback,
    emit_progress,
)
from app.orchestrator.router import IntentRouter
from app.orchestrator.schemas import OrchestrationResult
from app.registry import AgentRegistry, default_registry
from app.shared import (
    ExecutionContext,
    InMemoryMemoryStore,
    InMemorySessionStore,
    MemoryEntry,
    MemoryRole,
    MemoryStore,
    SessionStore,
    Telemetry,
    bind_execution_context,
    collect_model_calls,
)


class MultiAgentOrchestrator:
    """Coordinate routing, planning, domain execution, and grounded aggregation."""

    def __init__(
        self,
        *,
        dispatcher: AgentDispatcher,
        sessions: SessionStore | None = None,
        memory: MemoryStore | None = None,
        telemetry: Telemetry | None = None,
        registry: AgentRegistry = default_registry,
        router: IntentRouter | None = None,
        planner: ExecutionPlanner | None = None,
        aggregator: ResultAggregator | None = None,
    ) -> None:
        self.sessions = sessions or InMemorySessionStore()
        self.memory = memory or InMemoryMemoryStore()
        self.telemetry = telemetry or Telemetry()
        self.registry = registry
        self.router = router or IntentRouter(registry=registry)
        self.planner = planner or ExecutionPlanner(registry=registry)
        self.aggregator = aggregator or ResultAggregator()
        self.executor = PlanExecutor(
            dispatcher,
            self.telemetry,
            registry=registry,
        )

    async def run(
        self,
        *,
        message: str,
        principal_id: str,
        session_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        authorization: AuthorizationContext | None = None,
        progress: ProgressCallback | None = None,
    ) -> OrchestrationResult:
        started_at = perf_counter()
        session = self.sessions.create(owner_id=principal_id, session_id=session_id)
        effective_authorization = authorization or AuthorizationContext(
            principal_id=principal_id,
            scopes=frozenset({"ecommerce.read"}),
        )
        context = ExecutionContext.create(
            principal_id=principal_id,
            session_id=session.session_id,
            authorization=effective_authorization,
            request_id=request_id,
            trace_id=trace_id,
        )
        with bind_execution_context(context):
            with collect_model_calls() as model_calls:
                call_count = len(model_calls)
                routed = await self.router.route_async(message, session)
                if len(model_calls) > call_count:
                    await emit_progress(
                        progress,
                        OrchestrationProgress(
                            phase="model.routing.completed",
                            message="Model Router đã hoàn tất phân loại có cấu trúc.",
                        ),
                    )
                await emit_progress(
                    progress,
                    OrchestrationProgress(
                        phase="routing.completed",
                        message="Đã phân tích yêu cầu và xác định miền xử lý.",
                    ),
                )
                call_count = len(model_calls)
                plan = await self.planner.build_async(routed)
                if len(model_calls) > call_count:
                    await emit_progress(
                        progress,
                        OrchestrationProgress(
                            phase="model.planning.completed",
                            message="Model Planner đã đề xuất capability graph hợp lệ.",
                        ),
                    )
                await emit_progress(
                    progress,
                    OrchestrationProgress(
                        phase="planning.completed",
                        message=f"Đã tạo kế hoạch gồm {len(plan.steps)} bước.",
                    ),
                )
                agent_results = await self.executor.execute(
                    plan,
                    context,
                    progress,
                )
                call_count = len(model_calls)
                aggregation = await self.aggregator.aggregate_async(
                    intent=routed.intent,
                    results=agent_results,
                )
                if len(model_calls) > call_count:
                    await emit_progress(
                        progress,
                        OrchestrationProgress(
                            phase="model.synthesis.completed",
                            message="Model Synthesis đã tạo các claim gắn nguồn.",
                        ),
                    )
                await emit_progress(
                    progress,
                    OrchestrationProgress(
                        phase="aggregation.completed",
                        message="Đã tổng hợp câu trả lời có nguồn.",
                        status=aggregation.status,
                    ),
                )

        active_agent = self._active_agent(routed.intent, session.active_agent)
        state_patch: dict[str, object] = {}
        product_id = aggregation.selected_product_id or self._last_product_id(
            agent_results
        )
        if product_id is not None:
            state_patch["last_product_id"] = product_id
        self.sessions.update(
            owner_id=principal_id,
            session_id=session.session_id,
            active_agent=active_agent,
            last_intent=routed.intent,
            state_patch=state_patch,
        )
        self.memory.append(
            session_id=session.session_id,
            entry=MemoryEntry(role=MemoryRole.USER, summary=message[:2_000]),
        )
        self.memory.append(
            session_id=session.session_id,
            entry=MemoryEntry(
                role=MemoryRole.ASSISTANT,
                summary=aggregation.answer[:2_000],
                agent_id=active_agent,
            ),
        )
        duration_ms = (perf_counter() - started_at) * 1_000
        self.telemetry.record(
            context,
            component="orchestrator",
            operation="orchestrate",
            outcome=aggregation.status.value,
            duration_ms=duration_ms,
            attributes={
                "intent": routed.intent,
                "agent_steps": len(plan.steps),
                "model_calls": len(model_calls),
                "model_fallbacks": sum(1 for item in model_calls if item.fallback_used),
            },
        )
        return OrchestrationResult(
            status=aggregation.status,
            answer=aggregation.answer,
            request_id=context.request_id,
            trace_id=context.trace_id,
            session_id=context.session_id,
            intent=routed.intent,
            active_agent=active_agent,
            selected_product_id=aggregation.selected_product_id,
            plan=plan,
            agent_results=agent_results,
            provenance=aggregation.provenance,
            model_calls=tuple(model_calls),
            warnings=aggregation.warnings,
            duration_ms=duration_ms,
        )

    def _active_agent(self, intent: str, previous: str | None) -> str | None:
        return self.registry.active_agent_for_intent(intent) or previous

    @staticmethod
    def _last_product_id(agent_results: tuple[AgentResult, ...]) -> int | None:
        for result in reversed(agent_results):
            data = result.data
            candidates = data.get("products")
            ranking = data.get("ranking")
            search = data.get("search")
            retrieval = data.get("retrieval")
            if isinstance(ranking, dict):
                candidates = ranking.get("products")
            elif isinstance(search, dict):
                candidates = search.get("products")
            if isinstance(candidates, list) and candidates:
                first = candidates[0]
                if isinstance(first, dict) and isinstance(first.get("id"), int):
                    return int(first["id"])
            if isinstance(retrieval, dict):
                product = retrieval.get("product")
                if isinstance(product, dict) and isinstance(product.get("id"), int):
                    return int(product["id"])
        return None
