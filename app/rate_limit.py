"""Small in-memory rate limiters.

No external cache or database is used by design.  State is per process, which is
acceptable because the service is a single-process deployment for one owner.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque

from .config import LimitConfig, LoginLimitConfig


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    retry_after: int = 0


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: LimitConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_requests = limit.max_requests
        self.window_seconds = limit.window_seconds
        self._clock = clock
        self._events: dict[str, Deque[float]] = {}

    def _prune(self, bucket: Deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def check(self, key: str) -> LimitDecision:
        now = self._clock()
        bucket = self._events.setdefault(key, deque())
        self._prune(bucket, now)
        if len(bucket) >= self.max_requests:
            retry = max(1, math.ceil(bucket[0] + self.window_seconds - now))
            return LimitDecision(False, retry)
        bucket.append(now)
        if len(self._events) > 10_000:
            empty = [name for name, events in self._events.items() if not events]
            for name in empty:
                self._events.pop(name, None)
        return LimitDecision(True, 0)

    def reset(self) -> None:
        self._events.clear()


@dataclass
class _LoginBucket:
    failures: Deque[float]
    locked_until: float = 0.0


class LoginThrottle:
    """Block an address after repeated failures, with a separate lockout."""

    def __init__(
        self,
        config: LoginLimitConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._clock = clock
        self._buckets: dict[str, _LoginBucket] = {}

    def is_locked(self, key: str) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            return False
        now = self._clock()
        if bucket.locked_until > now:
            return True
        if bucket.locked_until:
            bucket.locked_until = 0.0
        cutoff = now - self.config.window_seconds
        while bucket.failures and bucket.failures[0] <= cutoff:
            bucket.failures.popleft()
        return False

    def record_failure(self, key: str) -> None:
        now = self._clock()
        bucket = self._buckets.setdefault(key, _LoginBucket(deque()))
        cutoff = now - self.config.window_seconds
        while bucket.failures and bucket.failures[0] <= cutoff:
            bucket.failures.popleft()
        bucket.failures.append(now)
        if len(bucket.failures) >= self.config.max_failures:
            bucket.locked_until = now + self.config.lockout_seconds
            bucket.failures.clear()
        if len(self._buckets) > 10_000:
            stale = [
                name
                for name, item in self._buckets.items()
                if not item.locked_until and not item.failures
            ]
            for name in stale:
                self._buckets.pop(name, None)

    def reset(self, key: str) -> None:
        self._buckets.pop(key, None)
