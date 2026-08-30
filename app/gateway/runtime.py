"""Composition root for one-process gateway and multi-agent runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis import Redis

from app.agent_gateway import AgentGateway
from app.agents import build_default_dispatcher
from app.core.config import Settings
from app.gateway.authorization import PrincipalAuthorizationRegistry
from app.gateway.rate_limit import InboundRateLimiter
from app.gateway.security import APIKeyAuthenticator
from app.gateway.turns import (
    RedisSessionTurnCoordinator,
    SessionTurnCoordinator,
    TurnCoordinator,
)
from app.knowledge import (
    DisabledKnowledgeStore,
    KnowledgeStore,
    QdrantKnowledgeStore,
)
from app.knowledge.runtime import (
    build_knowledge_embedder,
    versioned_knowledge_collection,
)
from app.mcp.catalog import build_default_mcp_router
from app.orchestrator import MultiAgentOrchestrator
from app.orchestrator.aggregator import ResultAggregator
from app.orchestrator.planner import ExecutionPlanner
from app.orchestrator.router import IntentRouter
from app.shared import (
    EmbeddingRuntime,
    InMemoryMemoryStore,
    InMemorySessionStore,
    MemoryStore,
    ModelRuntime,
    OpenAIModelRuntime,
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
    authorization_registry: PrincipalAuthorizationRegistry
    authentication_limiter: InboundRateLimiter
    rate_limiter: InboundRateLimiter
    telemetry: Telemetry
    config: Settings
    turns: TurnCoordinator
    redis_client: Any | None = None
    knowledge_store: KnowledgeStore = DisabledKnowledgeStore()
    model_runtime: ModelRuntime | None = None
    embedding_runtime: EmbeddingRuntime | None = None


def build_gateway_runtime(
    config: Settings,
    *,
    knowledge_store: KnowledgeStore | None = None,
) -> GatewayRuntime:
    """Build one coherent runtime without module-global mutable agent state."""

    telemetry = Telemetry()
    api_key = config.openai_api_key_value
    model_runtime: ModelRuntime | None = None
    if config.model_runtime_mode != "off" and api_key:
        model_runtime = OpenAIModelRuntime(
            api_key,
            telemetry=telemetry,
            timeout_seconds=config.openai_request_timeout_seconds,
            max_retries=config.openai_max_retries,
            max_output_tokens=config.openai_max_output_tokens,
            max_concurrency=config.openai_max_concurrency,
            circuit_failure_threshold=config.openai_circuit_failure_threshold,
            circuit_recovery_seconds=config.openai_circuit_recovery_seconds,
        )
    embedding_runtime: EmbeddingRuntime | None = None
    redis_client: Any | None = None
    sessions: SessionStore = InMemorySessionStore(
        ttl_seconds=config.session_ttl_seconds
    )
    memory: MemoryStore = InMemoryMemoryStore()
    turns: TurnCoordinator = SessionTurnCoordinator()
    configured_knowledge_store = (
        knowledge_store if knowledge_store is not None else DisabledKnowledgeStore()
    )
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
    if knowledge_store is None and config.knowledge_backend == "qdrant":
        embedding_runtime = build_knowledge_embedder(config)
        configured_knowledge_store = QdrantKnowledgeStore(
            config.qdrant_url,
            collection=versioned_knowledge_collection(
                config.qdrant_collection,
                embedding_runtime,
            ),
            api_key=config.qdrant_api_key.get_secret_value(),
            timeout_seconds=config.qdrant_timeout_seconds,
            embedder=embedding_runtime,
        )
    agent_gateway = AgentGateway(router=build_default_mcp_router())
    authenticator = APIKeyAuthenticator(config.gateway_api_keys.get_secret_value())
    authorization_registry = PrincipalAuthorizationRegistry.from_configuration(
        config.gateway_principal_policies
    )
    orchestrator = MultiAgentOrchestrator(
        dispatcher=build_default_dispatcher(
            agent_gateway,
            model_runtime=model_runtime,
            runtime_mode=config.model_runtime_mode,
            specialist_model=config.openai_specialist_model,
            reasoning_effort=config.openai_reasoning_effort,
        ),
        sessions=sessions,
        memory=memory,
        telemetry=telemetry,
        router=IntentRouter(
            model_runtime=model_runtime,
            runtime_mode=config.model_runtime_mode,
            model=config.openai_routing_model,
            reasoning_effort=config.openai_reasoning_effort,
        ),
        planner=ExecutionPlanner(
            model_runtime=model_runtime,
            runtime_mode=config.model_runtime_mode,
            model=config.openai_planning_model,
            reasoning_effort=config.openai_reasoning_effort,
        ),
        aggregator=ResultAggregator(
            model_runtime=model_runtime,
            runtime_mode=config.model_runtime_mode,
            model=config.openai_synthesis_model,
            reasoning_effort=config.openai_reasoning_effort,
        ),
    )
    return GatewayRuntime(
        orchestrator=orchestrator,
        agent_gateway=agent_gateway,
        authenticator=authenticator,
        authorization_registry=authorization_registry,
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
        knowledge_store=configured_knowledge_store,
        model_runtime=model_runtime,
        embedding_runtime=embedding_runtime,
    )
