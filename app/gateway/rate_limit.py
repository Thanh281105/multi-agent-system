"""Atomic in-process inbound rate limiting with a Redis-ready boundary."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from threading import Lock
from time import monotonic


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int = 0


class InboundRateLimiter:
    """Thread-safe fixed configuration/sliding-window limiter per principal."""

    def __init__(
        self,
        *,
        requests: int,
        window_seconds: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if requests < 1 or window_seconds < 1:
            raise ValueError("rate limit values must be positive")
        self.requests = requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, principal_id: str) -> RateLimitDecision:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[principal_id]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                retry_after = max(
                    1,
                    ceil(self.window_seconds - (now - events[0])),
                )
                return RateLimitDecision(
                    allowed=False,
                    limit=self.requests,
                    remaining=0,
                    retry_after_seconds=retry_after,
                )
            events.append(now)
            return RateLimitDecision(
                allowed=True,
                limit=self.requests,
                remaining=self.requests - len(events),
            )
