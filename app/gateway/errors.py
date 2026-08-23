"""Stable inbound gateway error types."""

from __future__ import annotations


class GatewayAPIError(Exception):
    """An expected client-facing failure without sensitive implementation detail."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
