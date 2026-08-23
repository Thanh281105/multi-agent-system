"""Composition root for one-process gateway and multi-agent runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis import Redis

from app.agent_gateway import AgentGateway
from app.agents import build_default_dispatcher
from app.core.config import Settings
from app.gateway.rate_limit import InboundRateLimiter
from app.gateway.security import APIKeyAuthenticator
from app.gateway.turns import (
    RedisSessionTurnCoordinator,
    SessionTurnCoordinator,
    TurnCoordinator,
)
from app.knowledge import QdrantKnowledgeStore
from app.mcp.catalog import build_default_mcp_router
from app.orchestrator import MultiAgentOrchestrator
from app.shared import (
    InMemoryMemoryStore,
    InMemorySessionStore,
    MemoryStore,
    RedisMemoryStore,
    RedisSessionStore,
    SessionStore,
    Telemetry,
)


@dataclass(frozen=True, slots=True)
class GatewayRuntime:
    """Long-lived adapters shared across HTTP requests."""

    orchestrator: MultiAgentOrchestrator
    agent_gateway: AgentGateway
    authenticator: APIKeyAuthenticator
    authentication_limiter: InboundRateLimiter
    rate_limiter: InboundRateLimiter
    telemetry: Telemetry
    config: Settings
    turns: TurnCoordinator
    redis_client: Any | None = None
    knowledge_store: QdrantKnowledgeStore | None = None


def build_gateway_runtime(config: Settings) -> GatewayRuntime:
    """Build one coherent runtime without module-global mutable agent state."""

    telemetry = Telemetry()
    redis_client: Any | None = None
    sessions: SessionStore = InMemorySessionStore(
        ttl_seconds=config.session_ttl_seconds
    )
    memory: MemoryStore = InMemoryMemoryStore()
    turns: TurnCoordinator = SessionTurnCoordinator()
    knowledge_store: QdrantKnowledgeStore | None = None
    if config.shared_state_backend == "redis":
        redis_client = Redis.from_url(
            config.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=config.redis_socket_timeout_seconds,
            socket_timeout=config.redis_socket_timeout_seconds,
        )
        sessions = RedisSessionStore(
            redis_client,
            ttl_seconds=config.session_ttl_seconds,
            key_prefix=config.redis_key_prefix,
        )
        memory = RedisMemoryStore(
            redis_client,
            ttl_seconds=config.session_ttl_seconds,
            key_prefix=config.redis_key_prefix,
        )
        turns = RedisSessionTurnCoordinator(
            redis_client,
            key_prefix=config.redis_key_prefix,
            lease_seconds=config.orchestration_timeout_seconds + 10,
        )
    if config.knowledge_backend == "qdrant":
        knowledge_store = QdrantKnowledgeStore(
            config.qdrant_url,
            collection=config.qdrant_collection,
            api_key=config.qdrant_api_key.get_secret_value(),
            timeout_seconds=config.qdrant_timeout_seconds,
        )
    agent_gateway = AgentGateway(
        router=build_default_mcp_router(
            knowledge_search=(
                knowledge_store.search if knowledge_store is not None else None
            )
        )
    )
    orchestrator = MultiAgentOrchestrator(
        dispatcher=build_default_dispatcher(agent_gateway),
        sessions=sessions,
        memory=memory,
        telemetry=telemetry,
    )
    return GatewayRuntime(
        orchestrator=orchestrator,
        agent_gateway=agent_gateway,
        authenticator=APIKeyAuthenticator(config.gateway_api_keys.get_secret_value()),
        authentication_limiter=InboundRateLimiter(
            requests=config.gateway_auth_attempt_requests,
            window_seconds=config.gateway_rate_limit_window_seconds,
        ),
        rate_limiter=InboundRateLimiter(
            requests=config.gateway_rate_limit_requests,
            window_seconds=config.gateway_rate_limit_window_seconds,
        ),
        telemetry=telemetry,
        config=config,
        turns=turns,
        redis_client=redis_client,
        knowledge_store=knowledge_store,
    )
