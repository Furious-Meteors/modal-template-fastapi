"""RateLimiter port: the in-memory and shared (Postgres) adapters behave the same where it matters."""
import asyncio

import pytest
import pytest_asyncio

from src.api.ratelimit import TokenBucketLimiter
from src.core.ports.rate_limiter import RateLimiter
from tests.conftest import BACKENDS, fresh_pg_pool, requires_postgres

LIMIT = 3


@pytest_asyncio.fixture(params=BACKENDS)
async def make_limiter(request):
    """Each call is a fresh 'container' with the same limit of LIMIT per window."""
    if request.param == "memory":
        yield lambda name="t", window=60: TokenBucketLimiter(rate_per_second=0.0001, burst=LIMIT)
        return
    from src.adapters.ratelimit.postgres import PostgresRateLimiter

    pool = await fresh_pg_pool()
    yield lambda name="t", window=60: PostgresRateLimiter(pool, name, LIMIT, window)
    await pool.close()


@pytest.mark.asyncio
async def test_satisfies_the_port(make_limiter):
    assert isinstance(make_limiter(), RateLimiter)


@pytest.mark.asyncio
async def test_allows_up_to_the_limit_then_says_how_long_to_wait(make_limiter):
    limiter = make_limiter()
    assert [await limiter.acquire("k") for _ in range(LIMIT)] == [0.0] * LIMIT
    wait = await limiter.acquire("k")
    assert wait >= 1 or (0 < wait)  # a positive retry hint
    assert TokenBucketLimiter.retry_after_header(wait)["Retry-After"].isdigit()


@pytest.mark.asyncio
async def test_keys_are_independent(make_limiter):
    limiter = make_limiter()
    for _ in range(LIMIT + 1):
        await limiter.acquire("a")
    assert await limiter.acquire("b") == 0.0


@requires_postgres
@pytest.mark.asyncio
async def test_the_limit_is_shared_across_containers():
    from src.adapters.ratelimit.postgres import PostgresRateLimiter

    pool = await fresh_pg_pool()
    try:
        containers = [PostgresRateLimiter(pool, "t", LIMIT) for _ in range(4)]
        results = await asyncio.gather(*(c.acquire("k") for c in containers for _ in range(2)))  # 8 concurrent calls
        assert sum(1 for r in results if r == 0.0) == LIMIT
    finally:
        await pool.close()


@requires_postgres
@pytest.mark.asyncio
async def test_the_window_rolls_over_and_old_windows_are_pruned():
    from src.adapters.ratelimit.postgres import PostgresRateLimiter

    pool = await fresh_pg_pool()
    try:
        limiter = PostgresRateLimiter(pool, "t", 1, window_seconds=1)
        for _ in range(30):  # whichever second boundary we straddle, a fresh window must eventually admit one
            if await limiter.acquire("k") == 0.0:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("window never rolled over")
        await pool.execute("INSERT INTO rate_limits VALUES ('old', 1, 5)")  # a window from 1970
        limiter._calls = 499
        await limiter.acquire("k")  # the 500th call prunes
        assert await pool.fetchval("SELECT count(*) FROM rate_limits WHERE bucket = 'old'") == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_a_database_outage_lets_requests_through_instead_of_failing_them(caplog):
    from src.adapters.ratelimit.postgres import PostgresRateLimiter

    class DeadPool:
        async def fetchrow(self, *a):
            raise ConnectionError("db down")

    limiter = PostgresRateLimiter(DeadPool(), "t", 1)
    with caplog.at_level("WARNING"):
        assert [await limiter.acquire("k") for _ in range(5)] == [0.0] * 5
    assert "allowing the request" in caplog.text


# ── wiring ──────────────────────────────────────────────────────────────────

def test_unknown_backend_and_missing_url_fail_at_startup(monkeypatch):
    from src import deps

    monkeypatch.setenv("RATE_LIMIT_BACKEND", "carrier-pigeon")
    with pytest.raises(ValueError, match="RATE_LIMIT_BACKEND"):
        deps._rate_limit_backend()
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(EnvironmentError, match="DATABASE_URL"):
        deps._rate_limit_backend()


def test_the_default_is_the_per_container_limiter(monkeypatch):
    from src import deps
    from src.api import ratelimit

    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    before = (ratelimit.ticket_limiter, ratelimit.webhook_limiter)
    deps._install_rate_limiters()
    assert (ratelimit.ticket_limiter, ratelimit.webhook_limiter) == before


@requires_postgres
@pytest.mark.asyncio
async def test_the_postgres_backend_is_installed_by_the_app_startup(monkeypatch):
    from src import deps
    from src.api import ratelimit
    from tests.conftest import TEST_DATABASE_URL

    await (await fresh_pg_pool()).close()
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("EVENT_BUS_BACKEND", "memory")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    original = (ratelimit.ticket_limiter, ratelimit.webhook_limiter)
    try:
        await deps.init_resources()  # the pool opens because the rate limiter alone needs one
        assert deps._pool is not None
        assert isinstance(ratelimit.ticket_limiter, RateLimiter)
        assert ratelimit.ticket_limiter is not original[0]
        assert await ratelimit.webhook_limiter.acquire("acme") == 0.0
    finally:
        await deps.close_resources()
        ratelimit.ticket_limiter, ratelimit.webhook_limiter = original
