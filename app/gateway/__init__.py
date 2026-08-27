"""Inbound HTTP gateway for authenticated, versioned platform access."""

from app.gateway.authorization import (
    PrincipalAuthorizationNotFoundError,
    PrincipalAuthorizationPolicy,
    PrincipalAuthorizationRegistry,
)
from app.gateway.runtime import GatewayRuntime, build_gateway_runtime

__all__ = [
    "GatewayRuntime",
    "PrincipalAuthorizationNotFoundError",
    "PrincipalAuthorizationPolicy",
    "PrincipalAuthorizationRegistry",
    "build_gateway_runtime",
]
