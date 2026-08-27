"""Versioned, deterministic skill selection for domain-agent tool calls.

The first selector is intentionally deterministic and auditable. A model-based
selector can replace it only after its selection quality is evaluated.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field


class SkillNotSelectedError(LookupError):
    """Raised when an action attempts to use a tool outside its selected skill."""


class SkillManifest(BaseModel):
    """A versioned capability with its focused MCP tool contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    skill_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    actions: frozenset[str]
    server_id: str
    tool_name: str
    instructions_ref: str


class SkillRegistry:
    """Select exactly the manifest that authorizes an action/tool invocation."""

    def __init__(self, manifests: Iterable[SkillManifest]) -> None:
        indexed: dict[tuple[str, str, str, str], SkillManifest] = {}
        for manifest in manifests:
            key = (
                manifest.agent_id,
                manifest.server_id,
                manifest.tool_name,
                manifest.skill_id,
            )
            if key in indexed:
                raise ValueError(f"duplicate skill manifest: {key}")
            indexed[key] = manifest
        self._manifests = indexed

    def select(
        self,
        *,
        agent_id: str,
        action: str,
        server_id: str,
        tool_name: str,
    ) -> SkillManifest:
        matches = [
            manifest
            for manifest in self._manifests.values()
            if manifest.agent_id == agent_id
            and manifest.server_id == server_id
            and manifest.tool_name == tool_name
            and action in manifest.actions
        ]
        if len(matches) != 1:
            raise SkillNotSelectedError(
                f"no selected skill for {agent_id}/{action}/{server_id}/{tool_name}"
            )
        return matches[0]


def _manifest(
    agent_id: str,
    skill_id: str,
    actions: set[str],
    server_id: str,
    tool_name: str,
) -> SkillManifest:
    return SkillManifest(
        agent_id=agent_id,
        skill_id=skill_id,
        version="1.0.0",
        actions=frozenset(actions),
        server_id=server_id,
        tool_name=tool_name,
        instructions_ref=f"skills/{agent_id}/{skill_id}@1.0.0",
    )


default_skill_registry = SkillRegistry(
    (
        _manifest(
            "product_agent",
            "search_products",
            {"product.search", "product.rank"},
            "product_db",
            "search_products",
        ),
        _manifest(
            "product_agent",
            "compare_products",
            {"product.compare", "product.follow_up"},
            "product_db",
            "compare_products",
        ),
        _manifest(
            "product_agent",
            "rank_products",
            {"product.rank"},
            "analytics",
            "rank_products",
        ),
        _manifest(
            "product_agent",
            "get_product_statistics",
            {"product.statistics"},
            "analytics",
            "get_product_statistics",
        ),
        _manifest(
            "review_agent",
            "get_product_reviews",
            {"review.get", "review.analyze", "review.summarize", "review.compare"},
            "review_db",
            "get_product_reviews",
        ),
        _manifest(
            "review_agent",
            "analyze_review_sentiment",
            {"review.analyze", "review.summarize", "review.compare"},
            "analytics",
            "analyze_review_sentiment",
        ),
        _manifest(
            "review_agent",
            "extract_review_aspects",
            {"review.analyze", "review.summarize", "review.compare"},
            "analytics",
            "extract_review_aspects",
        ),
        _manifest(
            "trust_agent",
            "get_product_reviews",
            {"trust.analyze", "trust.complaints", "trust.compare"},
            "review_db",
            "get_product_reviews",
        ),
        _manifest(
            "trust_agent",
            "analyze_review_trust",
            {"trust.analyze", "trust.complaints", "trust.compare"},
            "analytics",
            "analyze_review_trust",
        ),
        _manifest(
            "trust_agent",
            "detect_complaints",
            {"trust.analyze", "trust.complaints", "trust.compare"},
            "analytics",
            "detect_complaints",
        ),
        _manifest(
            "market_agent",
            "search_market_knowledge",
            {"market.analyze", "market.search"},
            "knowledge",
            "search_market_knowledge",
        ),
        _manifest(
            "market_agent",
            "analyze_market",
            {"market.analyze"},
            "analytics",
            "analyze_market",
        ),
    )
)
