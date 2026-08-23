"""In-process A2A dispatcher with a validated domain-agent allowlist."""

from __future__ import annotations

from collections.abc import Iterable

from app.agent_gateway import AgentGateway
from app.agents.base import DomainAgent
from app.agents.market import MarketAgent
from app.agents.product import ProductAgent
from app.agents.review import ReviewAgent
from app.agents.trust import TrustAgent
from app.contracts import AgentError, AgentMessage, AgentResult, TaskStatus


class AgentDispatcher:
    """Route typed messages without dynamic import or arbitrary dispatch."""

    def __init__(self, agents: Iterable[DomainAgent]) -> None:
        indexed: dict[str, DomainAgent] = {}
        for agent in agents:
            if agent.agent_id in indexed:
                raise ValueError(f"duplicate runtime agent: {agent.agent_id}")
            indexed[agent.agent_id] = agent
        if not indexed:
            raise ValueError("dispatcher must contain at least one agent")
        self._agents = indexed

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
        return await agent.execute(message)

    def agent_ids(self) -> tuple[str, ...]:
        return tuple(self._agents)


def build_default_dispatcher(gateway: AgentGateway) -> AgentDispatcher:
    return AgentDispatcher(
        (
            ProductAgent(gateway),
            ReviewAgent(gateway),
            TrustAgent(gateway),
            MarketAgent(gateway),
        )
    )
