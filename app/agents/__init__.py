"""Domain-specific intelligence agents for the modular monolith."""

from app.agents.dispatcher import (
    DEFAULT_AGENT_IDS,
    AgentDispatcher,
    build_default_dispatcher,
)
from app.agents.market import MarketAgent
from app.agents.product import ProductAgent
from app.agents.review import ReviewAgent
from app.agents.trust import TrustAgent

__all__ = [
    "AgentDispatcher",
    "DEFAULT_AGENT_IDS",
    "MarketAgent",
    "ProductAgent",
    "ReviewAgent",
    "TrustAgent",
    "build_default_dispatcher",
]
