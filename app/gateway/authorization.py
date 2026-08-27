"""Principal authorization policies resolved at the inbound gateway boundary."""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.contracts import AuthorizationContext
from app.contracts.a2a import ACTION_PATTERN, IDENTIFIER_PATTERN

_PRINCIPAL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class PrincipalAuthorizationPolicy(BaseModel):
    """Tenant and scopes assigned by a trusted gateway configuration source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(pattern=_PRINCIPAL_PATTERN.pattern)
    tenant_id: str = Field(default="default", pattern=IDENTIFIER_PATTERN)
    scopes: frozenset[str] = frozenset()

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not re.fullmatch(ACTION_PATTERN, scope) for scope in value):
            raise ValueError(
                "authorization scopes must use the action identifier format"
            )
        return value


class PrincipalAuthorizationNotFoundError(LookupError):
    """Raised when an authenticated principal has no authorization policy."""


class PrincipalAuthorizationRegistry:
    """Resolve explicit principal policies without granting implicit access."""

    def __init__(self, policies: Iterable[PrincipalAuthorizationPolicy]) -> None:
        indexed: dict[str, PrincipalAuthorizationPolicy] = {}
        for policy in policies:
            if policy.principal_id in indexed:
                raise ValueError(
                    f"duplicate principal authorization policy: {policy.principal_id}"
                )
            indexed[policy.principal_id] = policy
        self._policies = indexed

    @classmethod
    def from_configuration(cls, configuration: str) -> PrincipalAuthorizationRegistry:
        """Parse ``principal:tenant:scope1|scope2`` entries from trusted config."""

        policies: list[PrincipalAuthorizationPolicy] = []
        for raw_entry in configuration.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            parts = entry.split(":", 2)
            if len(parts) != 3:
                raise ValueError(
                    "GATEWAY_PRINCIPAL_POLICIES entries must use "
                    "principal:tenant:scope1|scope2"
                )
            principal_id, tenant_id, raw_scopes = (part.strip() for part in parts)
            scopes = frozenset(
                scope.strip() for scope in raw_scopes.split("|") if scope.strip()
            )
            policies.append(
                PrincipalAuthorizationPolicy(
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    scopes=scopes,
                )
            )
        return cls(policies)

    def resolve(self, principal_id: str) -> AuthorizationContext:
        try:
            policy = self._policies[principal_id]
        except KeyError as exc:
            raise PrincipalAuthorizationNotFoundError(principal_id) from exc
        return AuthorizationContext(
            principal_id=policy.principal_id,
            tenant_id=policy.tenant_id,
            scopes=policy.scopes,
        )

    def list(self) -> tuple[PrincipalAuthorizationPolicy, ...]:
        return tuple(self._policies.values())
