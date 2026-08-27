"""Transport-neutral MCP tool catalog and dispatcher.

The production modular monolith starts with an in-process adapter. A future
official MCP client/server transport can implement the same call boundary.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

ToolHandler = Callable[..., Any]


class MCPToolNotFoundError(LookupError):
    """Raised when a server/tool pair is not registered."""


class MCPDispatchError(RuntimeError):
    """Raised when a registered tool violates the MCP result contract."""


@dataclass(frozen=True, slots=True)
class MCPToolSpec:
    """Metadata required by the outbound authorization boundary."""

    server_id: str
    tool_name: str
    skill_id: str
    required_permission: str
    description: str
    handler: ToolHandler
    required_user_scope: str = "ecommerce.read"


class MCPRouter:
    """Explicit allowlisted server/tool registry."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], MCPToolSpec] = {}

    def register(self, spec: MCPToolSpec) -> None:
        key = (spec.server_id, spec.tool_name)
        if key in self._tools:
            raise ValueError(f"duplicate MCP tool: {spec.server_id}/{spec.tool_name}")
        self._tools[key] = spec

    def get_spec(self, server_id: str, tool_name: str) -> MCPToolSpec:
        try:
            return self._tools[(server_id, tool_name)]
        except KeyError as exc:
            raise MCPToolNotFoundError(f"{server_id}/{tool_name}") from exc

    def list_tools(self, server_id: str | None = None) -> tuple[MCPToolSpec, ...]:
        return tuple(
            spec
            for spec in self._tools.values()
            if server_id is None or spec.server_id == server_id
        )

    async def call(
        self,
        *,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        spec = self.get_spec(server_id, tool_name)
        if inspect.iscoroutinefunction(spec.handler):
            result = await spec.handler(**dict(arguments))
        else:
            result = await asyncio.to_thread(spec.handler, **dict(arguments))
        if not isinstance(result, dict):
            raise MCPDispatchError(
                f"MCP tool {server_id}/{tool_name} returned a non-object result"
            )
        return result
