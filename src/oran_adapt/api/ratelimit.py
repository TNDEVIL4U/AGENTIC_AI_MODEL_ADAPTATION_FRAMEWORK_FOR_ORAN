"""In-process token-bucket rate limits for the API.

Two limits, both per API replica (a limit shared across replicas belongs in the gateway or
ingress in front of them; docs/security.md):
- **requests per caller**: API_RATE_LIMIT_PER_MINUTE requests a minute per authenticated
  principal, with bursts up to API_RATE_LIMIT_BURST;
- **authentication failures per client address**: API_AUTH_FAILURE_LIMIT_PER_MINUTE failed
  attempts a minute; beyond that the address is refused before its credential is checked, so
  guessing keys or replaying tokens is slowed down.

A refused request gets 429 RATE_LIMITED with a ``Retry-After`` header. Buckets live in a bounded
LRU map (API_RATE_LIMIT_MAX_KEYS), so a flood of distinct keys cannot exhaust memory. A limit of
0 turns that limit off.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from oran_adapt.core.errors import RateLimitedError


class TokenBuckets:
    def __init__(
        self,
        per_minute: int,
        burst: int,
        *,
        max_keys: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = per_minute / 60.0
        self.capacity = float(max(burst, 1))
        self.max_keys = max_keys
        self.clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.rate > 0

    def _level(self, key: str, now: float) -> float:
        tokens, last = self._buckets.get(key, (self.capacity, now))
        return min(self.capacity, tokens + (now - last) * self.rate)

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` has a whole token again (0 when it has one now)."""
        with self._lock:
            level = self._level(key, self.clock())
        return 0.0 if level >= 1 or not self.enabled else (1 - level) / self.rate

    def take(self, key: str) -> float:
        """Spend a token for ``key``; returns 0, or the seconds to wait when none is left (the
        token is then not spent)."""
        if not self.enabled:
            return 0.0
        with self._lock:
            now = self.clock()
            level = self._level(key, now)
            if level < 1:
                self._buckets[key] = (level, now)
                self._buckets.move_to_end(key)
                return (1 - level) / self.rate
            self._buckets[key] = (level - 1, now)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return 0.0


def refuse(wait_s: float, what: str) -> RateLimitedError:
    return RateLimitedError(f"too many {what}; retry later",
                            retry_after_s=max(1, math.ceil(wait_s)))
