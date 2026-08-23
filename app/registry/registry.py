"""Validated agent metadata, capabilities, permissions, and MCP allowlists."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.a2a import IDENTIFIER_PATTERN


class AgentNotFoundError(LookupError):
    """Raised when a caller requests an agent absent from the registry."""


class RateLimitPolicy(BaseModel):
    """Per-agent outbound request budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: int = Field(default=120, ge=1, le=100_000)
    window_seconds: int = Field(default=60, ge=1, le=86_400)


class AgentBundle(BaseModel):
    """Immutable deployment and authorization metadata for one agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=500)
    capabilities: tuple[str, ...] = Field(min_length=1)
    skills: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()
    mcp_servers: frozenset[str] = frozenset()
    rate_limit: RateLimitPolicy = Field(default_factory=RateLimitPolicy)


class AgentRegistry:
    """In-memory registry with deterministic startup validation."""

    def __init__(self, bundles: Iterable[AgentBundle]) -> None:
        indexed: dict[str, AgentBundle] = {}
        for bundle in bundles:
            if bundle.agent_id in indexed:
                raise ValueError(f"duplicate agent ID: {bundle.agent_id}")
            if len(bundle.capabilities) != len(set(bundle.capabilities)):
                raise ValueError(f"duplicate capability in bundle: {bundle.agent_id}")
            if len(bundle.skills) != len(set(bundle.skills)):
                raise ValueError(f"duplicate skill in bundle: {bundle.agent_id}")
            indexed[bundle.agent_id] = bundle
        if not indexed:
            raise ValueError("registry must contain at least one agent")
        self._bundles = indexed

    def get(self, agent_id: str) -> AgentBundle:
        try:
            return self._bundles[agent_id]
        except KeyError as exc:
            raise AgentNotFoundError(agent_id) from exc

    def list(self) -> tuple[AgentBundle, ...]:
        return tuple(self._bundles.values())

    def find_by_capability(self, capability: str) -> tuple[AgentBundle, ...]:
        return tuple(
            bundle
            for bundle in self._bundles.values()
            if capability in bundle.capabilities
        )


def _default_bundles() -> tuple[AgentBundle, ...]:
    return (
        AgentBundle(
            agent_id="orchestrator",
            version="1.0.0",
            description="Phân tích ý định, lập kế hoạch và tổng hợp kết quả.",
            capabilities=("intent.routing", "task.planning", "result.aggregation"),
        ),
        AgentBundle(
            agent_id="product_agent",
            version="1.0.0",
            description="Tìm kiếm, so sánh và xếp hạng sản phẩm.",
            capabilities=("product.search", "product.compare", "product.rank"),
            skills=(
                "search_products",
                "compare_products",
                "rank_products",
                "get_product_statistics",
            ),
            permissions=frozenset({"product.read", "analytics.read"}),
            mcp_servers=frozenset({"product_db", "analytics"}),
        ),
        AgentBundle(
            agent_id="review_agent",
            version="1.0.0",
            description="Phân tích cảm xúc, khía cạnh và tóm tắt review.",
            capabilities=(
                "review.retrieve",
                "review.sentiment",
                "review.summarize",
                "review.compare",
            ),
            skills=(
                "get_product_reviews",
                "analyze_review_sentiment",
                "extract_review_aspects",
            ),
            permissions=frozenset({"review.read", "analytics.read"}),
            mcp_servers=frozenset({"review_db", "analytics"}),
        ),
        AgentBundle(
            agent_id="trust_agent",
            version="1.0.0",
            description="Phát hiện complaint, spam và đánh giá độ tin cậy.",
            capabilities=(
                "review.trust",
                "review.complaint",
                "review.anomaly",
                "trust.compare",
            ),
            skills=(
                "get_product_reviews",
                "analyze_review_trust",
                "detect_complaints",
            ),
            permissions=frozenset({"review.read", "trust.analyze"}),
            mcp_servers=frozenset({"review_db", "analytics"}),
        ),
        AgentBundle(
            agent_id="market_agent",
            version="1.0.0",
            description="Phân tích danh mục, giá và tín hiệu thị trường mẫu.",
            capabilities=("market.category", "market.price", "market.research"),
            skills=("analyze_market", "search_market_knowledge"),
            permissions=frozenset({"analytics.read", "knowledge.read"}),
            mcp_servers=frozenset({"analytics", "knowledge"}),
        ),
    )


default_registry = AgentRegistry(_default_bundles())
