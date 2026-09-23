"""Pure authorization policy for v2 conversation and sandbox resources."""

from __future__ import annotations

from types import MappingProxyType

from pydantic import ConfigDict, Field

from app.contracts import AuthorizationContext
from app.v2.contracts import IDENTIFIER_PATTERN, ConversationMode, V2Contract

DEMO_STORE_ID = "demo"

_MODE_READ_SCOPES = MappingProxyType(
    {
        ConversationMode.SHOPPER: frozenset({"ecommerce.read"}),
        ConversationMode.MERCHANT: frozenset({"ecommerce.read", "merchant.read"}),
    }
)
_MODE_WRITE_SCOPE = MappingProxyType(
    {
        ConversationMode.SHOPPER: "ecommerce.write",
        ConversationMode.MERCHANT: "merchant.write",
    }
)


class AuthorizationDeniedError(PermissionError):
    """The authenticated principal lacks a currently required scope."""

    code = "forbidden"

    def __init__(self) -> None:
        super().__init__("access denied")


class ResourceNotFoundError(LookupError):
    """Uniform response for missing and foreign-owned v2 resources."""

    code = "resource_not_found"

    def __init__(self) -> None:
        super().__init__("resource not found")


class AuthorityOverrideError(AuthorizationDeniedError):
    """A client or model attempted to widen a server-owned authority boundary."""

    code = "authority_override_forbidden"


class ResourceBinding(V2Contract):
    """Immutable ownership columns loaded from trusted persistence."""

    tenant_id: str = Field(pattern=IDENTIFIER_PATTERN)
    principal_id: str = Field(min_length=1, max_length=160)
    mode: ConversationMode
    store_id: str = Field(pattern=IDENTIFIER_PATTERN)


class ResourceAuthorization(V2Contract):
    """Request-local authorization derived only from current trusted identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: ResourceBinding
    scopes: frozenset[str]


def required_scopes_for_mode(
    mode: ConversationMode,
    *,
    write: bool = False,
) -> frozenset[str]:
    """Return the complete current-scope requirement for an operation."""

    scopes = _MODE_READ_SCOPES[mode]
    if write:
        scopes = scopes | {_MODE_WRITE_SCOPE[mode]}
    return scopes


def allowed_modes(authorization: AuthorizationContext) -> tuple[ConversationMode, ...]:
    """Derive selectable modes from current scopes in stable UI order."""

    return tuple(
        mode
        for mode in (ConversationMode.SHOPPER, ConversationMode.MERCHANT)
        if required_scopes_for_mode(mode) <= authorization.scopes
    )


def require_mode_access(
    authorization: AuthorizationContext,
    mode: ConversationMode,
    *,
    write: bool = False,
) -> None:
    """Reject a mode operation when any current required scope is absent."""

    if not required_scopes_for_mode(mode, write=write) <= authorization.scopes:
        raise AuthorizationDeniedError


def bind_request_authorization(
    authorization: AuthorizationContext,
    mode: ConversationMode,
    *,
    write: bool = False,
) -> ResourceAuthorization:
    """Bind current identity to a mode and the server-assigned demo store."""

    require_mode_access(authorization, mode, write=write)
    return ResourceAuthorization(
        binding=ResourceBinding(
            tenant_id=authorization.tenant_id,
            principal_id=authorization.principal_id,
            mode=mode,
            store_id=DEMO_STORE_ID,
        ),
        scopes=authorization.scopes,
    )


def authorize_resource_access(
    authorization: AuthorizationContext,
    binding: ResourceBinding,
    *,
    write: bool = False,
) -> ResourceAuthorization:
    """Authorize an owned resource while hiding whether foreign resources exist.

    Ownership is checked before resource-specific scope policy so callers expose
    the same not-found result for every cross-owner tenant/principal/store case.
    Current scopes are then checked afresh, including on replay and reads.
    """

    if (
        binding.tenant_id != authorization.tenant_id
        or binding.principal_id != authorization.principal_id
        or binding.store_id != DEMO_STORE_ID
    ):
        raise ResourceNotFoundError

    require_mode_access(authorization, binding.mode, write=write)
    return ResourceAuthorization(binding=binding, scopes=authorization.scopes)


def narrow_knowledge_acl(
    server_allowed_source_ids: frozenset[str],
    requested_source_ids: frozenset[str] | None,
) -> frozenset[str]:
    """Apply an optional caller filter without widening the server ACL."""

    if requested_source_ids is None:
        return server_allowed_source_ids
    if not requested_source_ids <= server_allowed_source_ids:
        raise AuthorityOverrideError
    return requested_source_ids
