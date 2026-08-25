"""In-process A2A dispatcher with a validated domain-agent allowlist."""

from __future__ import annotations

from collections.abc import Iterable

from app.agent_gateway import AgentGateway
from app.agents.base import DomainAgent
from app.agents.market import MarketAgent
from app.agents.product import ProductAgent
from app.agents.reasoning import AgentReasoner
from app.agents.review import ReviewAgent
from app.agents.trust import TrustAgent
from app.contracts import AgentError, AgentMessage, AgentResult, TaskStatus
from app.shared import ModelRuntime, ModelRuntimeMode, ReasoningEffort

DEFAULT_AGENT_TYPES: tuple[type[DomainAgent], ...] = (
    ProductAgent,
    ReviewAgent,
    TrustAgent,
    MarketAgent,
)
DEFAULT_AGENT_IDS: tuple[str, ...] = tuple(
    agent_type.agent_id for agent_type in DEFAULT_AGENT_TYPES
)


class AgentDispatcher:
    """Route typed messages without dynamic import or arbitrary dispatch."""

    def __init__(
        self,
        agents: Iterable[DomainAgent],
        *,
        reasoner: AgentReasoner | None = None,
    ) -> None:
        indexed: dict[str, DomainAgent] = {}
        for agent in agents:
            if agent.agent_id in indexed:
                raise ValueError(f"duplicate runtime agent: {agent.agent_id}")
            indexed[agent.agent_id] = agent
        if not indexed:
            raise ValueError("dispatcher must contain at least one agent")
        self._agents = indexed
        self._reasoner = reasoner

    async def dispatch(self, message: AgentMessage) -> AgentResult:
        agent = self._agents.get(message.target)
        if agent is None:
            return AgentResult(
                task_id=message.task_id,
                agent_id="orchestrator",
                status=TaskStatus.FAILED,
                errors=(
                    AgentError(
                        code="dispatcher.agent_not_found",
                        message="Không tìm thấy agent đích.",
                        source="orchestrator",
                    ),
                ),
            )
        result = await agent.execute(message)
        if self._reasoner is not None:
            return await self._reasoner.enrich(message, result)
        return result

    def agent_ids(self) -> tuple[str, ...]:
        return tuple(self._agents)


def build_default_dispatcher(
    gateway: AgentGateway,
    *,
    enabled_agents: Iterable[str] | None = None,
    model_runtime: ModelRuntime | None = None,
    runtime_mode: ModelRuntimeMode = "off",
    specialist_model: str = "gpt-5.4-nano",
    reasoning_effort: ReasoningEffort = "low",
) -> AgentDispatcher:
    requested_agents = (
        tuple(enabled_agents) if enabled_agents is not None else DEFAULT_AGENT_IDS
    )
    if len(requested_agents) != len(set(requested_agents)):
        raise ValueError("enabled agents must be unique")
    unknown_agents = set(requested_agents) - set(DEFAULT_AGENT_IDS)
    if unknown_agents:
        raise ValueError(f"unknown enabled agents: {sorted(unknown_agents)}")
    reasoner = (
        AgentReasoner(
            model_runtime,
            runtime_mode=runtime_mode,
            model=specialist_model,
            reasoning_effort=reasoning_effort,
        )
        if model_runtime is not None and runtime_mode != "off"
        else None
    )
    return AgentDispatcher(
        tuple(
            agent_type(gateway)
            for agent_type in DEFAULT_AGENT_TYPES
            if agent_type.agent_id in requested_agents
        ),
        reasoner=reasoner,
    )
