"""Authorization policy tests for identity, mode, store, and ACL isolation."""

import pytest

from app.contracts import AuthorizationContext
from app.v2.authorization import (
    DEMO_STORE_ID,
    AuthorityOverrideError,
    AuthorizationDeniedError,
    ResourceBinding,
    ResourceNotFoundError,
    allowed_modes,
    authorize_resource_access,
    bind_request_authorization,
    narrow_knowledge_acl,
    require_mode_access,
)
from app.v2.contracts import ConversationMode


def _auth(
    *,
    principal_id: str = "alice",
    tenant_id: str = "tenant-a",
    scopes: frozenset[str] = frozenset({"ecommerce.read"}),
) -> AuthorizationContext:
    return AuthorizationContext(
        principal_id=principal_id,
        tenant_id=tenant_id,
        scopes=scopes,
    )


def _binding(
    *,
    principal_id: str = "alice",
    tenant_id: str = "tenant-a",
    mode: ConversationMode = ConversationMode.SHOPPER,
    store_id: str = DEMO_STORE_ID,
) -> ResourceBinding:
    return ResourceBinding(
        principal_id=principal_id,
        tenant_id=tenant_id,
        mode=mode,
        store_id=store_id,
    )


def test_allowed_modes_are_derived_only_from_current_scopes() -> None:
    assert allowed_modes(_auth()) == (ConversationMode.SHOPPER,)
    assert allowed_modes(
        _auth(scopes=frozenset({"ecommerce.read", "merchant.read"}))
    ) == (ConversationMode.SHOPPER, ConversationMode.MERCHANT)
    assert (
        allowed_modes(_auth(scopes=frozenset({"merchant.read", "merchant.write"})))
        == ()
    )


def test_principal_name_never_grants_mode_or_role() -> None:
    named_admin = _auth(
        principal_id="merchant_admin",
        scopes=frozenset({"ecommerce.read"}),
    )

    with pytest.raises(AuthorizationDeniedError):
        require_mode_access(named_admin, ConversationMode.MERCHANT)


def test_mode_read_and_write_scopes_are_all_required() -> None:
    require_mode_access(_auth(), ConversationMode.SHOPPER)
    with pytest.raises(AuthorizationDeniedError):
        require_mode_access(_auth(), ConversationMode.SHOPPER, write=True)

    merchant_read = _auth(scopes=frozenset({"ecommerce.read", "merchant.read"}))
    require_mode_access(merchant_read, ConversationMode.MERCHANT)
    with pytest.raises(AuthorizationDeniedError):
        require_mode_access(merchant_read, ConversationMode.MERCHANT, write=True)

    merchant_write = _auth(
        scopes=frozenset({"ecommerce.read", "merchant.read", "merchant.write"})
    )
    require_mode_access(merchant_write, ConversationMode.MERCHANT, write=True)


def test_binding_uses_the_server_assigned_demo_store() -> None:
    authorization = bind_request_authorization(_auth(), ConversationMode.SHOPPER)

    assert authorization.binding.store_id == DEMO_STORE_ID
    assert authorization.binding.principal_id == "alice"
    assert authorization.binding.tenant_id == "tenant-a"
    with pytest.raises(TypeError):
        bind_request_authorization(  # type: ignore[call-arg]
            _auth(), ConversationMode.SHOPPER, store_id="foreign-store"
        )


@pytest.mark.parametrize(
    "binding",
    [
        _binding(principal_id="bob"),
        _binding(tenant_id="tenant-b"),
        _binding(store_id="foreign-store"),
    ],
)
def test_cross_owner_resources_use_uniform_not_found_semantics(
    binding: ResourceBinding,
) -> None:
    with pytest.raises(ResourceNotFoundError) as denied:
        authorize_resource_access(_auth(), binding)

    assert denied.value.code == "resource_not_found"
    assert str(denied.value) == "resource not found"


def test_cross_owner_check_precedes_mode_scope_failure() -> None:
    foreign_merchant = _binding(
        principal_id="bob",
        mode=ConversationMode.MERCHANT,
    )

    with pytest.raises(ResourceNotFoundError):
        authorize_resource_access(_auth(scopes=frozenset()), foreign_merchant)


def test_current_scopes_are_rechecked_after_access_was_previously_allowed() -> None:
    binding = _binding(mode=ConversationMode.MERCHANT)
    granted = _auth(
        scopes=frozenset({"ecommerce.read", "merchant.read", "merchant.write"})
    )
    authorize_resource_access(granted, binding, write=True)

    revoked = _auth(scopes=frozenset({"ecommerce.read"}))
    with pytest.raises(AuthorizationDeniedError) as denied:
        authorize_resource_access(revoked, binding)
    assert denied.value.code == "forbidden"


def test_knowledge_acl_can_only_be_narrowed() -> None:
    server_acl = frozenset({"source_public", "source_tenant_a"})

    assert narrow_knowledge_acl(server_acl, None) == server_acl
    assert narrow_knowledge_acl(server_acl, frozenset({"source_public"})) == frozenset(
        {"source_public"}
    )
    with pytest.raises(AuthorityOverrideError) as denied:
        narrow_knowledge_acl(
            server_acl,
            frozenset({"source_public", "source_tenant_b"}),
        )
    assert denied.value.code == "authority_override_forbidden"
