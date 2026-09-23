"""Correlation, safe error envelopes, access logs, and HTTP metrics."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.gateway.errors import GatewayAPIError
from app.gateway.rate_limit import RateLimitDecision
from app.gateway.runtime import GatewayRuntime
from app.gateway.schemas import GatewayErrorDetail, GatewayErrorResponse

logger = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^req_[a-zA-Z0-9_-]{3,120}$")
_TRACE_ID = re.compile(r"^trace_[a-zA-Z0-9_-]{3,120}$")
_KNOWN_METRIC_PATHS = {
    "/api/v2/chat",
    "/api/v2/chat/stream",
    "/health",
    "/livez",
    "/readyz",
    "/metrics",
    "/",
}
# Sonner 2.0.8 injects these two static style elements at module evaluation.
# Keep the package pinned and re-run browser CSP checks before changing either hash.
_SONNER_STYLE_HASHES = (
    "'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='",
    "'sha256-StEaX+se6YS7pqjzrzMIA0KaX9zF/8zAhvQXZAe5epY='",
)


def install_gateway_middleware(application: FastAPI) -> None:
    """Install correlation before routes and API-specific exception rendering."""

    application.add_exception_handler(GatewayAPIError, _gateway_error_handler)
    application.add_exception_handler(RequestValidationError, _validation_handler)
    application.add_exception_handler(HTTPException, _http_error_handler)

    @application.middleware("http")
    async def correlation_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = _correlation_id(
            request.headers.get("X-Request-ID"),
            pattern=_REQUEST_ID,
            prefix="req",
        )
        trace_id = _correlation_id(
            None,
            pattern=_TRACE_ID,
            prefix="trace",
        )
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        request.state.csp_nonce = secrets.token_urlsafe(18)
        started_at = perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "HTTP_UNHANDLED method=%s path=%s request_id=%s trace_id=%s "
                "error_type=%s",
                request.method,
                _safe_path(request.url.path),
                request_id,
                trace_id,
                type(exc).__name__,
            )
            if request.url.path.startswith("/api/v2/"):
                response = _error_response(
                    request,
                    status_code=500,
                    code="v2.internal_error",
                    message="Không thể xử lý yêu cầu lúc này.",
                    retryable=False,
                )
            else:
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "Internal Server Error"},
                )
        duration_ms = (perf_counter() - started_at) * 1_000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = trace_id
        _set_rate_limit_headers(request, response)
        _set_security_headers(request, response)
        _record_http_metric(request, response.status_code, duration_ms)
        logger.info(
            "HTTP_REQUEST method=%s path=%s status=%d request_id=%s "
            "trace_id=%s latency_ms=%d",
            request.method,
            _safe_path(request.url.path),
            response.status_code,
            request_id,
            trace_id,
            int(duration_ms),
        )
        return response


async def _gateway_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, GatewayAPIError):
        raise TypeError("unexpected gateway exception type")
    headers: dict[str, str] = {}
    if exc.status_code == 401:
        headers["WWW-Authenticate"] = "ApiKey"
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return _error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        retryable=exc.retryable,
        headers=headers,
    )


async def _validation_handler(
    request: Request,
    exc: Exception,
) -> Response:
    if not isinstance(exc, RequestValidationError):
        raise TypeError("unexpected validation exception type")
    if not request.url.path.startswith("/api/v2/"):
        return await request_validation_exception_handler(request, exc)
    safe_errors: tuple[dict[str, object], ...] = tuple(
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "type": str(error.get("type", "validation_error")),
            "message": str(error.get("msg", "Giá trị không hợp lệ.")),
        }
        for error in exc.errors()
    )
    return _error_response(
        request,
        status_code=422,
        code="v2.validation_failed",
        message="Payload không hợp lệ.",
        validation_errors=safe_errors,
    )


async def _http_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, HTTPException):
        raise TypeError("unexpected HTTP exception type")
    if not request.url.path.startswith("/api/v2/"):
        return await http_exception_handler(request, exc)
    return _error_response(
        request,
        status_code=exc.status_code,
        code="v2.http_error",
        message="Yêu cầu không thể được xử lý.",
    )


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    validation_errors: tuple[dict[str, object], ...] = (),
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = GatewayErrorResponse(
        error=GatewayErrorDetail(
            code=code,
            message=message,
            request_id=getattr(request.state, "request_id", "req_unavailable"),
            trace_id=getattr(request.state, "trace_id", "trace_unavailable"),
            retryable=retryable,
            validation_errors=validation_errors,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def _correlation_id(value: str | None, *, pattern: re.Pattern[str], prefix: str) -> str:
    if value and pattern.fullmatch(value):
        return value
    return f"{prefix}_{uuid4().hex}"


def _set_rate_limit_headers(request: Request, response: Response) -> None:
    decision = getattr(request.state, "rate_limit", None)
    if not isinstance(decision, RateLimitDecision):
        return
    response.headers["X-RateLimit-Limit"] = str(decision.limit)
    response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
    if decision.retry_after_seconds:
        response.headers["Retry-After"] = str(decision.retry_after_seconds)


def _set_security_headers(request: Request, response: Response) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    script_sources = "'self'"
    style_sources = "'self'"
    if nonce := getattr(request.state, "csp_nonce", None):
        style_sources += f" 'nonce-{nonce}'"
    style_sources += " " + " ".join(_SONNER_STYLE_HASHES)
    if request.url.path in {"/docs", "/redoc"}:
        script_sources += " 'unsafe-inline' https://cdn.jsdelivr.net"
        style_sources += " 'unsafe-inline' https://cdn.jsdelivr.net"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src {script_sources}; "
        f"style-src {style_sources}; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    )
    if request.url.path.startswith("/api/") or request.url.path == "/metrics":
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"
    elif request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"

    runtime = getattr(request.app.state, "gateway_runtime", None)
    if isinstance(runtime, GatewayRuntime) and runtime.config.app_env == "production":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )


def _record_http_metric(
    request: Request,
    status_code: int,
    duration_ms: float,
) -> None:
    runtime = getattr(request.app.state, "gateway_runtime", None)
    if not isinstance(runtime, GatewayRuntime):
        return
    path = _safe_path(request.url.path)
    labels = {
        "method": request.method,
        "path": path,
        "status_class": f"{status_code // 100}xx",
    }
    runtime.telemetry.metrics.increment("http_requests_total", labels=labels)
    runtime.telemetry.metrics.observe_duration(
        "http_request_duration_seconds",
        duration_ms / 1_000,
        labels={"method": request.method, "path": path},
    )


def _safe_path(path: str) -> str:
    return path if path in _KNOWN_METRIC_PATHS else "other"
