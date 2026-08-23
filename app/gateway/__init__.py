"""Inbound HTTP gateway for authenticated, versioned platform access."""

from app.gateway.runtime import GatewayRuntime, build_gateway_runtime

__all__ = ["GatewayRuntime", "build_gateway_runtime"]
