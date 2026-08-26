"""Regression tests for sessions, memory, correlation, and telemetry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app.shared import (
    ExecutionContext,
    InMemoryMemoryStore,
    InMemorySessionStore,
    MemoryEntry,
    SessionExpiredError,
    SessionOwnershipError,
    Telemetry,
    bind_execution_context,
)
from app.shared.context import current_execution_context


def test_session_store_binds_session_to_owner_and_supports_agent_pinning() -> None:
    store = InMemorySessionStore()
    session = store.create(owner_id="user-a", session_id="sess_owned_123")

    updated = store.update(
        owner_id="user-a",
        session_id=session.session_id,
        active_agent="product_agent",
        last_intent="product.search",
        state_patch={"product_id": 1},
    )

    assert updated.active_agent == "product_agent"
    assert updated.last_intent == "product.search"
    assert updated.state == {"product_id": 1}
    assert updated.revision == 1
    with pytest.raises(SessionOwnershipError):
        store.get(owner_id="user-b", session_id=session.session_id)


def test_session_store_expires_state_with_an_aware_clock() -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    store = InMemorySessionStore(ttl_seconds=60, clock=lambda: now[0])
    session = store.create(owner_id="user-a", session_id="sess_expiry_123")
    now[0] += timedelta(seconds=61)

    with pytest.raises(SessionExpiredError):
        store.get(owner_id="user-a", session_id=session.session_id)


def test_session_updates_are_atomic_across_threads() -> None:
    store = InMemorySessionStore()
    session = store.create(owner_id="user-a", session_id="sess_atomic_123")

    def update(index: int) -> None:
        store.update(
            owner_id="user-a",
            session_id=session.session_id,
            state_patch={f"key_{index}": index},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(update, range(20)))

    current = store.get(owner_id="user-a", session_id=session.session_id)
    assert current.revision == 20
    assert len(current.state) == 20


def test_short_term_memory_is_bounded_and_returns_latest_entries() -> None:
    memory = InMemoryMemoryStore(max_entries_per_session=2)
    for summary in ("turn one", "turn two", "turn three"):
        memory.append(
            session_id="sess_memory_123",
            entry=MemoryEntry(role="user", summary=summary),
        )

    assert [entry.summary for entry in memory.recent(session_id="sess_memory_123")] == [
        "turn two",
        "turn three",
    ]


def test_execution_context_binding_is_scoped() -> None:
    context = ExecutionContext.create(
        principal_id="user-a",
        session_id="sess_context_123",
    )
    assert current_execution_context() is None

    with bind_execution_context(context):
        assert current_execution_context() == context

    assert current_execution_context() is None


def test_telemetry_propagates_ids_and_does_not_capture_unlisted_values() -> None:
    telemetry = Telemetry()
    context = ExecutionContext.create(
        principal_id="user-a",
        session_id="sess_trace_123",
        request_id="req_trace_123",
        trace_id="trace_trace_123",
    ).child(agent_id="product_agent", task_id="task_trace_123")

    with telemetry.span(
        context,
        component="product_agent",
        operation="search_products",
        attributes={"result_count": 2},
    ):
        pass

    event = telemetry.events(trace_id="trace_trace_123")[0]
    assert event.request_id == "req_trace_123"
    assert event.task_id == "task_trace_123"
    assert event.attributes == {"result_count": 2}
    metrics = telemetry.metrics.render_prometheus()
    assert "agent_operations_total" in metrics
    assert "# TYPE agent_operation_duration_seconds histogram" in metrics
    assert (
        'agent_operation_duration_seconds_bucket{component="product_agent",le="+Inf"'
        in metrics
    )
    assert "user-a" not in event.model_dump_json()


def test_duration_histogram_exports_cumulative_finite_buckets() -> None:
    telemetry = Telemetry()
    telemetry.metrics.observe_duration("work_duration_seconds", 0.02)
    telemetry.metrics.observe_duration("work_duration_seconds", 0.2)

    metrics = telemetry.metrics.render_prometheus()

    assert "# TYPE work_duration_seconds histogram" in metrics
    assert 'work_duration_seconds_bucket{le="0.01"} 0' in metrics
    assert 'work_duration_seconds_bucket{le="0.025"} 1' in metrics
    assert 'work_duration_seconds_bucket{le="0.25"} 2' in metrics
    assert 'work_duration_seconds_bucket{le="+Inf"} 2' in metrics
    assert "work_duration_seconds_count 2" in metrics
    assert "work_duration_seconds_sum 0.220000" in metrics


@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan")])
def test_duration_histogram_rejects_invalid_observations(value: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        Telemetry().metrics.observe_duration("work_duration_seconds", value)
