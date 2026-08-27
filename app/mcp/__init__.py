"""MCP-compatible in-process tool boundary used by the modular monolith."""

from app.mcp.router import (
    MCPDispatchError,
    MCPRouter,
    MCPToolNotFoundError,
    MCPToolSpec,
)

__all__ = [
    "MCPDispatchError",
    "MCPRouter",
    "MCPToolNotFoundError",
    "MCPToolSpec",
]
