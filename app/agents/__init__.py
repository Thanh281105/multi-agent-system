"""Domain-specific intelligence agents for the modular monolith."""

from app.agents.dispatcher import AgentDispatcher, build_default_dispatcher
from app.agents.market import MarketAgent
from app.agents.product import ProductAgent
from app.agents.review import ReviewAgent
from app.agents.trust import TrustAgent

__all__ = [
    "AgentDispatcher",
    "MarketAgent",
    "ProductAgent",
    "ReviewAgent",
    "TrustAgent",
    "build_default_dispatcher",
]
