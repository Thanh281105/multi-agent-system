"""Validated agent metadata, capabilities, permissions, and MCP allowlists."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ExecutionStep
from app.contracts.a2a import IDENTIFIER_PATTERN

INTENT_PATTERN = r"^[a-z][a-z0-9_.-]{1,127}$"


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
    follow_up_intent: str | None = Field(default=None, pattern=INTENT_PATTERN)


PlanBuilder = Callable[[dict[str, Any]], tuple[ExecutionStep, ...]]
CapabilityBuilder = Callable[[dict[str, Any]], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class IntentManifest:
    """Domain-owned compiler hooks for one supported routed intent."""

    intent: str
    active_agent_id: str | None
    build_steps: PlanBuilder
    expected_capabilities: CapabilityBuilder


class AgentRegistry:
    """In-memory registry with deterministic startup validation."""

    def __init__(
        self,
        bundles: Iterable[AgentBundle],
        *,
        intent_manifests: Iterable[IntentManifest] = (),
    ) -> None:
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

        intents: dict[str, IntentManifest] = {}
        for manifest in intent_manifests:
            if manifest.intent in intents:
                raise ValueError(f"duplicate intent manifest: {manifest.intent}")
            if (
                manifest.active_agent_id is not None
                and manifest.active_agent_id not in indexed
            ):
                raise ValueError(
                    "intent manifest references unknown agent: "
                    f"{manifest.active_agent_id}"
                )
            intents[manifest.intent] = manifest
        for bundle in indexed.values():
            if (
                bundle.follow_up_intent is not None
                and bundle.follow_up_intent not in intents
            ):
                raise ValueError(
                    "agent follow-up intent is not registered: "
                    f"{bundle.follow_up_intent}"
                )
        self._intents = intents

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

    def intent_manifest(self, intent: str) -> IntentManifest | None:
        """Return the registered domain plan compiler for an intent, if any."""

        return self._intents.get(intent)

    def active_agent_for_intent(self, intent: str) -> str | None:
        manifest = self.intent_manifest(intent)
        return manifest.active_agent_id if manifest is not None else None

    def follow_up_intent(self, agent_id: str) -> str | None:
        return self.get(agent_id).follow_up_intent


def _default_bundles() -> tuple[AgentBundle, ...]:
    return (
        AgentBundle(
            agent_id="orchestrator",
            version="1.0.0",
            description="Định tuyến, lập kế hoạch và tổng hợp trợ lý sách Tiki.",
            capabilities=("intent.routing", "task.planning", "result.aggregation"),
        ),
        AgentBundle(
            agent_id="product_agent",
            version="1.0.0",
            description=(
                "Book Catalog Agent: tìm, so sánh và xếp hạng sách trong snapshot."
            ),
            capabilities=("product.search", "product.compare", "product.rank"),
            skills=(
                "search_products",
                "compare_products",
                "rank_products",
                "get_product_statistics",
            ),
            permissions=frozenset({"product.read", "analytics.read"}),
            mcp_servers=frozenset({"product_db", "analytics"}),
            follow_up_intent="product.follow_up",
        ),
        AgentBundle(
            agent_id="review_agent",
            version="1.0.0",
            description="Phân tích cảm xúc và khía cạnh review sách đã lấy mẫu.",
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
            follow_up_intent="review.summary",
        ),
        AgentBundle(
            agent_id="trust_agent",
            version="1.0.0",
            description=(
                "Mô tả complaint và tín hiệu chất lượng văn bản, "
                "không kết luận giả mạo."
            ),
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
            follow_up_intent="trust.complaints",
        ),
        AgentBundle(
            agent_id="market_agent",
            version="1.0.0",
            description=(
                "Thống kê cắt ngang category, author, publisher, giá và rating "
                "trong snapshot sách."
            ),
            capabilities=("market.category", "market.price", "market.research"),
            skills=("analyze_market", "search_market_knowledge"),
            permissions=frozenset({"analytics.read", "knowledge.read"}),
            mcp_servers=frozenset({"analytics", "knowledge"}),
            follow_up_intent="market.analyze",
        ),
    )


def _default_intent_manifests() -> tuple[IntentManifest, ...]:
    # Import lazily so domain plan definitions can depend on registry contracts
    # without coupling the registry module to the orchestrator implementation.
    from app.registry.default_intents import build_default_intent_manifests

    return build_default_intent_manifests()


default_registry = AgentRegistry(
    _default_bundles(),
    intent_manifests=_default_intent_manifests(),
)
