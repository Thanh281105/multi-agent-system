"""Production-like modular multi-agent orchestration service."""

from __future__ import annotations

from time import perf_counter

from app.agents import AgentDispatcher
from app.contracts import AgentResult
from app.orchestrator.aggregator import ResultAggregator
from app.orchestrator.executor import PlanExecutor
from app.orchestrator.planner import ExecutionPlanner
from app.orchestrator.router import IntentRouter
from app.orchestrator.schemas import OrchestrationResult
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
        router: IntentRouter | None = None,
        planner: ExecutionPlanner | None = None,
        aggregator: ResultAggregator | None = None,
    ) -> None:
        self.sessions = sessions or InMemorySessionStore()
        self.memory = memory or InMemoryMemoryStore()
        self.telemetry = telemetry or Telemetry()
        self.router = router or IntentRouter()
        self.planner = planner or ExecutionPlanner()
        self.aggregator = aggregator or ResultAggregator()
        self.executor = PlanExecutor(dispatcher, self.telemetry)

    async def run(
        self,
        *,
        message: str,
        principal_id: str,
        session_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> OrchestrationResult:
        started_at = perf_counter()
        session = self.sessions.create(owner_id=principal_id, session_id=session_id)
        context = ExecutionContext.create(
            principal_id=principal_id,
            session_id=session.session_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        with bind_execution_context(context):
            routed = self.router.route(message, session)
            plan = self.planner.build(routed)
            agent_results = await self.executor.execute(plan, context)
            aggregation = self.aggregator.aggregate(
                intent=routed.intent,
                results=agent_results,
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
            warnings=aggregation.warnings,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _active_agent(intent: str, previous: str | None) -> str | None:
        if intent.startswith("product") or intent == "multi.recommendation":
            return "product_agent"
        if intent.startswith("review"):
            return "review_agent"
        if intent.startswith("trust"):
            return "trust_agent"
        if intent.startswith("market"):
            return "market_agent"
        return previous

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
