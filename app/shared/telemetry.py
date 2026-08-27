"""Dependency-light tracing and metric primitives for the modular monolith."""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from math import isfinite
from threading import RLock
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.a2a import utc_now
from app.shared.context import ExecutionContext

_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_DURATION_BUCKETS_SECONDS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


class TraceEvent(BaseModel):
    """Redacted trace event with platform-wide correlation IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime = Field(default_factory=utc_now)
    request_id: str
    trace_id: str
    session_id: str
    task_id: str
    agent_id: str
    component: str
    operation: str
    outcome: str
    duration_ms: float = Field(ge=0)
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)


class MetricRegistry:
    """Thread-safe counters and bounded Prometheus duration histograms."""

    def __init__(self) -> None:
        self._counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self._duration_histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]],
            tuple[int, float, tuple[int, ...]],
        ] = {}
        self._lock = RLock()

    def increment(
        self,
        name: str,
        *,
        labels: Mapping[str, str] | None = None,
        amount: int = 1,
    ) -> None:
        self._validate_name(name)
        if amount < 0:
            raise ValueError("counter amount must be non-negative")
        key = (name, self._labels(labels))
        with self._lock:
            self._counters[key] += amount

    def observe_duration(
        self,
        name: str,
        duration_seconds: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._validate_name(name)
        if not isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("duration must be finite and non-negative")
        key = (name, self._labels(labels))
        with self._lock:
            count, total, bucket_counts = self._duration_histograms.get(
                key,
                (0, 0.0, (0,) * len(_DURATION_BUCKETS_SECONDS)),
            )
            self._duration_histograms[key] = (
                count + 1,
                total + duration_seconds,
                tuple(
                    bucket_count + int(duration_seconds <= boundary)
                    for bucket_count, boundary in zip(
                        bucket_counts,
                        _DURATION_BUCKETS_SECONDS,
                        strict=True,
                    )
                ),
            )

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            previous_name: str | None = None
            for (name, labels), value in sorted(self._counters.items()):
                if name != previous_name:
                    lines.append(f"# TYPE {name} counter")
                    previous_name = name
                lines.append(f"{name}{self._format_labels(labels)} {value}")
            previous_name = None
            for (name, labels), (count, total, buckets) in sorted(
                self._duration_histograms.items()
            ):
                if name != previous_name:
                    lines.append(f"# TYPE {name} histogram")
                    previous_name = name
                for boundary, bucket_count in zip(
                    _DURATION_BUCKETS_SECONDS,
                    buckets,
                    strict=True,
                ):
                    bucket_labels = self._labels(
                        {**dict(labels), "le": f"{boundary:g}"}
                    )
                    lines.append(
                        f"{name}_bucket{self._format_labels(bucket_labels)} "
                        f"{bucket_count}"
                    )
                infinite_labels = self._labels({**dict(labels), "le": "+Inf"})
                lines.append(
                    f"{name}_bucket{self._format_labels(infinite_labels)} {count}"
                )
                lines.append(f"{name}_count{self._format_labels(labels)} {count}")
                lines.append(f"{name}_sum{self._format_labels(labels)} {total:.6f}")
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _labels(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((labels or {}).items()))

    @staticmethod
    def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        encoded = ",".join(
            f'{key}="{MetricRegistry._escape_label(value)}"' for key, value in labels
        )
        return "{" + encoded + "}"

    @staticmethod
    def _escape_label(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError(f"invalid metric name: {name}")


class Telemetry:
    """Collect bounded local traces and aggregate service metrics."""

    def __init__(
        self,
        *,
        trace_capacity: int = 5_000,
        metrics: MetricRegistry | None = None,
    ) -> None:
        if trace_capacity < 1:
            raise ValueError("trace_capacity must be positive")
        self.metrics = metrics or MetricRegistry()
        self._events: deque[TraceEvent] = deque(maxlen=trace_capacity)
        self._lock = RLock()

    @contextmanager
    def span(
        self,
        context: ExecutionContext,
        *,
        component: str,
        operation: str,
        attributes: Mapping[str, str | int | float | bool] | None = None,
    ) -> Iterator[None]:
        started_at = perf_counter()
        outcome = "success"
        try:
            yield
        except Exception:
            outcome = "error"
            raise
        finally:
            duration_ms = (perf_counter() - started_at) * 1_000
            self.record(
                context,
                component=component,
                operation=operation,
                outcome=outcome,
                duration_ms=duration_ms,
                attributes=attributes,
            )

    def record(
        self,
        context: ExecutionContext,
        *,
        component: str,
        operation: str,
        outcome: str,
        duration_ms: float,
        attributes: Mapping[str, str | int | float | bool] | None = None,
    ) -> None:
        event = TraceEvent(
            request_id=context.request_id,
            trace_id=context.trace_id,
            session_id=context.session_id,
            task_id=context.task_id,
            agent_id=context.agent_id,
            component=component,
            operation=operation,
            outcome=outcome,
            duration_ms=duration_ms,
            attributes=dict(attributes or {}),
        )
        with self._lock:
            self._events.append(event)
        labels = {"component": component, "operation": operation, "outcome": outcome}
        self.metrics.increment("agent_operations_total", labels=labels)
        self.metrics.observe_duration(
            "agent_operation_duration_seconds",
            duration_ms / 1_000,
            labels={"component": component, "operation": operation},
        )

    def events(self, *, trace_id: str | None = None) -> tuple[TraceEvent, ...]:
        with self._lock:
            return tuple(
                event
                for event in self._events
                if trace_id is None or event.trace_id == trace_id
            )
