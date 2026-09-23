"""Shared safe error envelope used by the v2 API and operations plane."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GatewayErrorDetail(BaseModel):
    """Stable safe error independent of internal exception classes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    request_id: str
    trace_id: str
    retryable: bool = False
    validation_errors: tuple[dict[str, object], ...] = ()


class GatewayErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: GatewayErrorDetail
