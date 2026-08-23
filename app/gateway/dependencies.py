"""FastAPI dependencies for inbound authentication and rate limiting."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, Request

from app.gateway.errors import GatewayAPIError
from app.gateway.rate_limit import RateLimitDecision
from app.gateway.runtime import GatewayRuntime


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    principal_id: str
    rate_limit: RateLimitDecision


def get_runtime(request: Request) -> GatewayRuntime:
    runtime = getattr(request.app.state, "gateway_runtime", None)
    if not isinstance(runtime, GatewayRuntime):
        raise RuntimeError("gateway runtime is not initialized")
    return runtime


async def authorize_request(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> PrincipalContext:
    """Authenticate first, then atomically consume the principal request budget."""

    runtime = get_runtime(request)
    peer = request.client.host if request.client is not None else "unknown"
    authentication_decision = runtime.authentication_limiter.check(f"peer:{peer}")
    request.state.rate_limit = authentication_decision
    if not authentication_decision.allowed:
        raise GatewayAPIError(
            status_code=429,
            code="gateway.authentication_rate_limited",
            message="Quá nhiều lần xác thực; vui lòng thử lại sau.",
            retryable=True,
            retry_after_seconds=authentication_decision.retry_after_seconds,
        )
    principal_id = runtime.authenticator.authenticate(x_api_key)
    decision = runtime.rate_limiter.check(principal_id)
    request.state.principal_id = principal_id
    request.state.rate_limit = decision
    if not decision.allowed:
        raise GatewayAPIError(
            status_code=429,
            code="gateway.rate_limited",
            message="Đã vượt quá giới hạn yêu cầu; vui lòng thử lại sau.",
            retryable=True,
            retry_after_seconds=decision.retry_after_seconds,
        )
    return PrincipalContext(principal_id=principal_id, rate_limit=decision)
