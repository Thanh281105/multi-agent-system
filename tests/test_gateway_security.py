"""Deterministic unit tests for inbound authentication and atomic quotas."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.gateway.errors import GatewayAPIError
from app.gateway.rate_limit import InboundRateLimiter
from app.gateway.security import APIKeyAuthenticator
from app.gateway.turns import SessionTurnCoordinator


def test_api_key_authenticator_maps_keys_without_accepting_near_matches() -> None:
    authenticator = APIKeyAuthenticator("alice:alice-secret-key,bob:bob-secret-key")

    assert authenticator.authenticate("alice-secret-key") == "alice"
    assert authenticator.authenticate("bob-secret-key") == "bob"
    with pytest.raises(GatewayAPIError) as missing:
        authenticator.authenticate(None)
    with pytest.raises(GatewayAPIError):
        authenticator.authenticate("alice-secret-keY")
    assert missing.value.status_code == 401


@pytest.mark.parametrize(
    "configuration",
    ["", "alice:short", "bad principal:long-enough-key", "alice-no-separator"],
)
def test_api_key_authenticator_rejects_invalid_startup_configuration(
    configuration: str,
) -> None:
    with pytest.raises(ValueError):
        APIKeyAuthenticator(configuration)


def test_rate_limiter_is_atomic_per_principal_and_resets() -> None:
    now = [100.0]
    limiter = InboundRateLimiter(
        requests=5,
        window_seconds=60,
        clock=lambda: now[0],
    )

    with ThreadPoolExecutor(max_workers=20) as executor:
        decisions = list(executor.map(lambda _: limiter.check("alice"), range(20)))

    assert sum(decision.allowed for decision in decisions) == 5
    assert all(decision.limit == 5 for decision in decisions)
    assert limiter.check("bob").allowed is True

    now[0] = 161.0
    reset = limiter.check("alice")
    assert reset.allowed is True
    assert reset.remaining == 4


@pytest.mark.asyncio
async def test_session_turn_coordinator_serializes_only_matching_sessions() -> None:
    coordinator = SessionTurnCoordinator()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with coordinator.turn("sess_same_123"):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second() -> None:
        await first_entered.wait()
        async with coordinator.turn("sess_same_123"):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert order == ["first-enter", "first-exit", "second-enter"]
    assert coordinator.active_session_count() == 0
