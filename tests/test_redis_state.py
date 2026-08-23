"""Redis shared-state adapter tests using an in-process protocol fake."""

import asyncio
from dataclasses import replace

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.gateway.runtime import build_gateway_runtime
from app.gateway.turns import RedisSessionTurnCoordinator, SessionTurnBusyError
from app.main import create_app
from app.shared import (
    MemoryEntry,
    MemoryRole,
    RedisMemoryStore,
    RedisSessionStore,
    SessionNotFoundError,
    SessionOwnershipError,
)


def test_redis_session_store_preserves_owner_revision_and_ttl() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    store = RedisSessionStore(client, ttl_seconds=120, key_prefix="test")

    created = store.create(owner_id="principal-a", session_id="sess_redis_123")
    updated = store.update(
        owner_id="principal-a",
        session_id=created.session_id,
        active_agent="product_agent",
        state_patch={"last_product_id": 1},
    )

    assert updated.revision == 1
    assert updated.active_agent == "product_agent"
    assert updated.state == {"last_product_id": 1}
    assert 0 < client.ttl("test:session:sess_redis_123") <= 120
    with pytest.raises(SessionOwnershipError):
        store.get(owner_id="principal-b", session_id=created.session_id)

    store.delete(owner_id="principal-a", session_id=created.session_id)
    with pytest.raises(SessionNotFoundError):
        store.get(owner_id="principal-a", session_id=created.session_id)


def test_redis_memory_store_is_bounded_and_expiring() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    memory = RedisMemoryStore(
        client,
        max_entries_per_session=3,
        ttl_seconds=90,
        key_prefix="test",
    )

    for index in range(4):
        memory.append(
            session_id="sess_memory_redis",
            entry=MemoryEntry(role=MemoryRole.USER, summary=f"turn-{index}"),
        )

    assert [item.summary for item in memory.recent(session_id="sess_memory_redis")] == [
        "turn-1",
        "turn-2",
        "turn-3",
    ]
    assert 0 < client.ttl("test:memory:sess_memory_redis") <= 90


def test_runtime_selects_redis_adapters_without_eager_network_access() -> None:
    config = Settings(
        _env_file=None,
        app_env="test",
        shared_state_backend="redis",
        redis_url="redis://127.0.0.1:6399/15",
        gateway_api_keys="test:test-secret-key",
    )

    runtime = build_gateway_runtime(config)

    assert isinstance(runtime.orchestrator.sessions, RedisSessionStore)
    assert isinstance(runtime.orchestrator.memory, RedisMemoryStore)
    assert runtime.redis_client is not None
    assert isinstance(runtime.turns, RedisSessionTurnCoordinator)


def test_production_requires_redis_shared_state() -> None:
    with pytest.raises(ValueError, match="SHARED_STATE_BACKEND=redis"):
        Settings(
            _env_file=None,
            app_env="production",
            gateway_api_keys="production:strong-production-key",
            legacy_chat_enabled=False,
            shared_state_backend="memory",
        )


@pytest.mark.asyncio
async def test_redis_turn_coordinator_serializes_across_workers() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    first = RedisSessionTurnCoordinator(
        client,
        key_prefix="test",
        wait_seconds=0.05,
        poll_seconds=0.01,
    )
    second = RedisSessionTurnCoordinator(
        client,
        key_prefix="test",
        wait_seconds=0.05,
        poll_seconds=0.01,
    )

    async with first.turn("sess_shared_123"):
        with pytest.raises(SessionTurnBusyError):
            async with second.turn("sess_shared_123"):
                pytest.fail("second worker must not enter the same session turn")

    async with second.turn("sess_shared_123"):
        await asyncio.sleep(0)


def test_redis_failure_is_sanitized_and_reported_by_readiness() -> None:
    config = Settings(
        _env_file=None,
        app_env="test",
        gateway_api_keys="test:test-secret-key",
    )
    application = create_app(config)
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    server.connected = False
    runtime = application.state.gateway_runtime
    runtime.orchestrator.sessions = RedisSessionStore(client, key_prefix="test")
    application.state.gateway_runtime = replace(runtime, redis_client=client)
    http = TestClient(application)

    failed_request = http.post(
        "/api/v1/chat",
        headers={"X-API-Key": "test-secret-key"},
        json={"message": "Tìm tai nghe"},
    )
    readiness = http.get("/readyz")

    assert failed_request.status_code == 503
    assert failed_request.json()["error"]["code"] == (
        "gateway.shared_state_unavailable"
    )
    assert readiness.status_code == 503
    assert readiness.json()["checks"] == {
        "runtime": "ok",
        "database": "ok",
        "redis": "failed",
    }
