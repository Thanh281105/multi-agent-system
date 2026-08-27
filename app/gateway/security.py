"""Constant-time API-key authentication mapped to stable principal IDs."""

from __future__ import annotations

import hmac
import re

from app.gateway.errors import GatewayAPIError

_PRINCIPAL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class APIKeyAuthenticator:
    """Parse principal:key pairs once and compare provided keys in constant time."""

    def __init__(self, configuration: str) -> None:
        credentials: list[tuple[str, str]] = []
        principals: set[str] = set()
        secrets: set[str] = set()
        for raw_entry in configuration.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            principal, separator, secret = entry.partition(":")
            principal = principal.strip()
            secret = secret.strip()
            if (
                not separator
                or not _PRINCIPAL_PATTERN.fullmatch(principal)
                or not 8 <= len(secret) <= 256
            ):
                raise ValueError(
                    "GATEWAY_API_KEYS entries must use principal:secret "
                    "with an 8-256 character secret"
                )
            if principal in principals:
                raise ValueError(f"duplicate gateway principal: {principal}")
            if secret in secrets:
                raise ValueError("gateway API keys must be unique")
            principals.add(principal)
            secrets.add(secret)
            credentials.append((principal, secret))
        if not credentials:
            raise ValueError("at least one gateway API key is required")
        self._credentials = tuple(credentials)

    def authenticate(self, provided_key: str | None) -> str:
        matched_principal: str | None = None
        candidate = provided_key or ""
        if len(candidate) > 256:
            candidate = ""
        for principal, configured_key in self._credentials:
            if hmac.compare_digest(candidate, configured_key):
                matched_principal = principal
        if matched_principal is None:
            raise GatewayAPIError(
                status_code=401,
                code="gateway.authentication_failed",
                message="API key không hợp lệ hoặc chưa được cung cấp.",
            )
        return matched_principal
