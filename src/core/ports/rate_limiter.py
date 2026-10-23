"""
src/core/ports/rate_limiter.py
==============================
Request-rate limiting for the endpoints that guard themselves (ticket issuing, webhooks).

acquire() takes one unit for `key` and says how long the caller must wait: 0 means allowed.
A per-process adapter limits each container separately; a shared adapter (Postgres) limits
the whole deployment however many containers Modal starts. Keys must never be shared by
unrelated callers (see src/api/ratelimit.py).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RateLimiter(Protocol):
    async def acquire(self, key: str) -> float:
        """0.0 if allowed, otherwise the seconds until a retry can succeed."""
        ...
