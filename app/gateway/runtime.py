"""Composition root for one-process gateway and multi-agent runtime state."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent_gateway import AgentGateway
from app.agents import build_default_dispatcher
from app.core.config import Settings
from app.gateway.rate_limit import InboundRateLimiter
from app.gateway.security import APIKeyAuthenticator
from app.gateway.turns import SessionTurnCoordinator
from app.mcp.catalog import build_default_mcp_router
from app.orchestrator import MultiAgentOrchestrator
from app.shared import InMemorySessionStore, Telemetry


@dataclass(frozen=True, slots=True)
class GatewayRuntime:
    """Long-lived, process-local adapters shared across HTTP requests."""

    orchestrator: MultiAgentOrchestrator
    agent_gateway: AgentGateway
    authenticator: APIKeyAuthenticator
    authentication_limiter: InboundRateLimiter
    rate_limiter: InboundRateLimiter
    telemetry: Telemetry
    config: Settings
    turns: SessionTurnCoordinator


def build_gateway_runtime(config: Settings) -> GatewayRuntime:
    """Build one coherent runtime without module-global mutable agent state."""

    telemetry = Telemetry()
    agent_gateway = AgentGateway(router=build_default_mcp_router())
    orchestrator = MultiAgentOrchestrator(
        dispatcher=build_default_dispatcher(agent_gateway),
        sessions=InMemorySessionStore(ttl_seconds=config.session_ttl_seconds),
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
        turns=SessionTurnCoordinator(),
    )
