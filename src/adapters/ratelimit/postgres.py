"""
src/adapters/ratelimit/postgres.py
==================================
RateLimiter shared by every container (RATE_LIMIT_BACKEND=postgres): a fixed window counter,
one statement per call.

Differences from the in-memory token bucket, deliberately: no burst allowance (the limit is
"N per window", so a caller can use up to 2N around a window boundary) and one database round
trip per check (about two wire round trips behind a transaction pooler). Use it when the limit
is a real quota across containers; keep the in-memory limiter for abuse protection.

If the database is unreachable the limiter fails OPEN: the request is let through (and logged),
so rate limiting never adds a new way for the service to go down.
"""
from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)

_HIT = """
INSERT INTO rate_limits (bucket, window_start, hits)
VALUES ($1, floor(extract(epoch FROM now()) / $2)::bigint * $2, 1)
ON CONFLICT (bucket, window_start) DO UPDATE SET hits = rate_limits.hits + 1
RETURNING hits, $2 - (extract(epoch FROM now())::bigint % $2) AS wait
"""
_PRUNE = "DELETE FROM rate_limits WHERE window_start < extract(epoch FROM now())::bigint - 3600"
_PRUNE_EVERY = 500  # calls per container between cleanups of finished windows


class PostgresRateLimiter:
    def __init__(self, pool: asyncpg.Pool, name: str, limit: int, window_seconds: int = 60) -> None:
        self._pool, self._name, self._limit, self._window = pool, name, limit, window_seconds
        self._calls = 0

    async def acquire(self, key: str) -> float:
        self._calls += 1
        try:
            row = await self._pool.fetchrow(_HIT, f"{self._name}:{key}", self._window)
            if self._calls % _PRUNE_EVERY == 0:
                await self._pool.execute(_PRUNE)
        except Exception:
            logger.warning("Rate limiter could not reach the database; allowing the request", exc_info=True)
            return 0.0
        return 0.0 if row["hits"] <= self._limit else float(max(1, row["wait"]))
