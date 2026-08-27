"""Security boundary for every outbound skill/MCP invocation."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic, perf_counter
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.a2a import IDENTIFIER_PATTERN, AgentError, AuthorizationContext
from app.mcp import MCPDispatchError, MCPRouter, MCPToolNotFoundError
from app.registry import AgentNotFoundError, AgentRegistry, default_registry


class GatewayRequest(BaseModel):
    """Authenticated invocation request from a domain agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=IDENTIFIER_PATTERN)
    task_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    trace_id: str = Field(pattern=IDENTIFIER_PATTERN)
    authorization: AuthorizationContext
    action: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    server_id: str = Field(pattern=IDENTIFIER_PATTERN)
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class GatewayResponse(BaseModel):
    """Safe result returned to a skill without leaking gateway internals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    audit_id: str = Field(pattern=IDENTIFIER_PATTERN)
    data: dict[str, Any] = Field(default_factory=dict)
    error: AgentError | None = None
    duration_ms: float = Field(ge=0)


class AuditRecord(BaseModel):
    """Redacted outbound audit event retained by the local adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str
    agent_id: str
    task_id: str
    request_id: str
    trace_id: str
    server_id: str
    tool_name: str
    agent_version: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    principal_ref: str | None = None
    tenant_id: str | None = None
    argument_keys: tuple[str, ...]
    outcome: str
    error_code: str | None = None
    duration_ms: float = Field(ge=0)


@dataclass(frozen=True, slots=True)
class _LimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class SlidingWindowRateLimiter:
    """Thread-safe local limiter; replaceable by a Redis adapter in deployment."""

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, *, key: str, requests: int, window_seconds: int) -> _LimitDecision:
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= requests:
                retry_after = max(1, int(window_seconds - (now - events[0])))
                return _LimitDecision(False, retry_after)
            events.append(now)
            return _LimitDecision(True)


class AgentGateway:
    """Enforce registry bundles before forwarding requests to MCP tools."""

    def __init__(
        self,
        *,
        router: MCPRouter,
        registry: AgentRegistry = default_registry,
        skill_registry: Any | None = None,
        limiter: SlidingWindowRateLimiter | None = None,
        audit_capacity: int = 2_000,
    ) -> None:
        if audit_capacity < 1:
            raise ValueError("audit_capacity must be positive")
        if skill_registry is None and registry is not default_registry:
            raise ValueError(
                "custom AgentRegistry requires an explicit SkillRegistry"
            )
        self.router = router
        self.registry = registry
        self.skill_registry = skill_registry or _default_skill_registry()
        self.limiter = limiter or SlidingWindowRateLimiter()
        self._audit: deque[AuditRecord] = deque(maxlen=audit_capacity)
        self._audit_lock = Lock()

    async def execute(self, request: GatewayRequest) -> GatewayResponse:
        started_at = perf_counter()
        audit_id = f"audit_{uuid4().hex}"
        error: AgentError | None = None
        data: dict[str, Any] = {}
        bundle = None
        selected_skill = None

        try:
            bundle = self.registry.get(request.agent_id)
            spec = self.router.get_spec(request.server_id, request.tool_name)
            if request.server_id not in bundle.mcp_servers:
                error = self._error(
                    "gateway.server_forbidden",
                    "Agent không được phép truy cập MCP server này.",
                )
            elif spec.skill_id not in bundle.skills:
                error = self._error(
                    "gateway.skill_forbidden",
                    "Skill chưa được cấp cho agent.",
                )
            elif spec.required_permission not in bundle.permissions:
                error = self._error(
                    "gateway.permission_denied",
                    "Agent thiếu quyền thực thi tool.",
                )
            else:
                selected_skill = self.skill_registry.select(
                    agent_id=request.agent_id,
                    action=request.action,
                    server_id=request.server_id,
                    tool_name=request.tool_name,
                )
                if selected_skill.skill_id != spec.skill_id:
                    error = self._error(
                        "gateway.skill_contract_mismatch",
                        "Skill đã chọn không khớp với MCP tool.",
                    )
                elif spec.required_user_scope not in request.authorization.scopes:
                    error = self._error(
                        "gateway.user_scope_denied",
                        "Principal không có scope cần thiết để gọi tool.",
                    )
                else:
                    decision = self.limiter.check(
                        key=request.agent_id,
                        requests=bundle.rate_limit.requests,
                        window_seconds=bundle.rate_limit.window_seconds,
                    )
                    if not decision.allowed:
                        error = self._error(
                            "gateway.rate_limited",
                            "Agent đã vượt quá giới hạn gọi tool.",
                            retryable=True,
                        )
                    else:
                        data = await self.router.call(
                            server_id=request.server_id,
                            tool_name=request.tool_name,
                            arguments=request.arguments,
                        )
        except AgentNotFoundError:
            error = self._error(
                "gateway.unknown_agent",
                "Agent không tồn tại trong registry.",
            )
        except MCPToolNotFoundError:
            error = self._error(
                "gateway.unknown_tool",
                "MCP tool không tồn tại.",
            )
        except LookupError:
            error = self._error(
                "gateway.skill_not_selected",
                "Action không được phép dùng tool này.",
            )
        except (MCPDispatchError, TypeError, ValueError):
            error = self._error(
                "gateway.invalid_tool_result",
                "MCP tool trả kết quả không hợp lệ.",
            )
        except Exception:
            error = self._error(
                "gateway.downstream_unavailable",
                "Không thể truy xuất dữ liệu từ dịch vụ downstream.",
                retryable=True,
            )

        duration_ms = (perf_counter() - started_at) * 1_000
        self._append_audit(
            AuditRecord(
                audit_id=audit_id,
                agent_id=request.agent_id,
                task_id=request.task_id,
                request_id=request.request_id,
                trace_id=request.trace_id,
                server_id=request.server_id,
                tool_name=request.tool_name,
                agent_version=bundle.version if bundle is not None else None,
                skill_id=selected_skill.skill_id
                if selected_skill is not None
                else None,
                skill_version=(
                    selected_skill.version if selected_skill is not None else None
                ),
                principal_ref=request.authorization.principal_id[:12],
                tenant_id=request.authorization.tenant_id,
                argument_keys=tuple(sorted(request.arguments)),
                outcome="success" if error is None else "denied_or_failed",
                error_code=error.code if error else None,
                duration_ms=duration_ms,
            )
        )
        return GatewayResponse(
            ok=error is None,
            audit_id=audit_id,
            data=data,
            error=error,
            duration_ms=duration_ms,
        )

    def audit_records(self) -> tuple[AuditRecord, ...]:
        with self._audit_lock:
            return tuple(self._audit)

    def _append_audit(self, record: AuditRecord) -> None:
        with self._audit_lock:
            self._audit.append(record)

    @staticmethod
    def _error(code: str, message: str, *, retryable: bool = False) -> AgentError:
        return AgentError(
            code=code,
            message=message,
            source="agent_gateway",
            retryable=retryable,
        )


def _default_skill_registry() -> Any:
    """Import after package initialization to avoid an AgentGateway import cycle."""

    from app.agents.skill_manifest import default_skill_registry

    return default_skill_registry
