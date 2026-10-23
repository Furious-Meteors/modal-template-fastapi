"""
src/api/ratelimit.py
====================
Small in-memory token-bucket limiter.

Per container: with N containers the effective limit is up to N x the
configured rate. That is the right tool for abuse protection (a runaway client,
a ticket-farming loop) but not a hard global quota — enforce those at the edge.

Keys must never be shared by unrelated callers. The limits wired in this
template therefore use the authenticated user (ticket endpoint), the provider
(webhooks) and the connection (WebSocket messages) — never the client IP,
which behind a proxy can be one address for everybody.
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from typing import Callable

from src.utils.config import (
    RATE_TICKET_PER_MINUTE,
    RATE_WEBHOOK_PER_MINUTE,
    RATE_WS_MESSAGES_PER_SECOND,
)


class TokenBucketLimiter:
    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = rate_per_second
        self.burst = burst
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: "OrderedDict[str, tuple[float, float]]" = OrderedDict()

    def check(self, key: str) -> float:
        """Take one token. Returns 0 if allowed, else seconds until one is available."""
        now = self._clock()
        tokens, last = self._buckets.pop(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self.rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            allowed = 0.0
        else:
            self._buckets[key] = (tokens, now)
            allowed = (1.0 - tokens) / self.rate
        while len(self._buckets) > self._max_keys:  # drop the least recently used key
            self._buckets.popitem(last=False)
        return allowed

    async def acquire(self, key: str) -> float:
        """RateLimiter port (src/core/ports/rate_limiter.py): same answer as check()."""
        return self.check(key)

    @staticmethod
    def retry_after_header(wait_seconds: float) -> dict[str, str]:
        return {"Retry-After": str(max(1, math.ceil(wait_seconds)))}


def _per_minute(per_minute: int, burst: int) -> TokenBucketLimiter:
    return TokenBucketLimiter(per_minute / 60.0, burst)


# Module-level so tests can swap them; endpoints read them at call time.
ticket_limiter = _per_minute(RATE_TICKET_PER_MINUTE, burst=max(5, RATE_TICKET_PER_MINUTE // 3))
webhook_limiter = _per_minute(RATE_WEBHOOK_PER_MINUTE, burst=max(50, RATE_WEBHOOK_PER_MINUTE // 10))


def new_ws_message_limiter() -> TokenBucketLimiter:
    """One per WebSocket connection."""
    return TokenBucketLimiter(RATE_WS_MESSAGES_PER_SECOND, burst=RATE_WS_MESSAGES_PER_SECOND * 2)
