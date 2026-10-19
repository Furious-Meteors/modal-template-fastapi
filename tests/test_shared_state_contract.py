"""Webhook idempotency and ticket replay stores: one contract, every backend."""
import asyncio
import time

import pytest
import pytest_asyncio

from src.adapters.events.memory import InMemoryTicketStore, InMemoryWebhookStore
from src.core.ports.ticket_store import TicketReplayStore
from src.core.ports.webhook_store import WebhookEventStore
from tests.asgi_helpers import wait_until
from tests.conftest import BACKENDS, TEST_DATABASE_URL, fresh_pg_pool, requires_postgres


@pytest_asyncio.fixture(params=BACKENDS)
async def stores(request):
    """(make_webhook_store, make_ticket_store); each call is a fresh 'container'."""
    if request.param == "memory":
        shared_hooks, shared_tickets = {}, {}

        def hooks(ttl=86_400):
            s = InMemoryWebhookStore(ttl_seconds=ttl)
            return s

        yield hooks, lambda: InMemoryTicketStore()
        return
    from src.adapters.events.postgres import PostgresTicketStore, PostgresWebhookStore
    from src.adapters.persistence.postgres import create_pool

    pools = [await fresh_pg_pool()]

    async def _pool():
        p = await create_pool(TEST_DATABASE_URL)
        pools.append(p)
        return p

    # sync factories need a pool already: build two up front
    pool_a, pool_b = await _pool(), await _pool()
    made = {"i": 0}

    def hooks(ttl=86_400):
        made["i"] += 1
        return PostgresWebhookStore(pool_a if made["i"] % 2 else pool_b, ttl)

    def tickets():
        made["i"] += 1
        return PostgresTicketStore(pool_a if made["i"] % 2 else pool_b)

    yield hooks, tickets
    for p in pools:
        await p.close()


def test_memory_stores_satisfy_ports():
    assert isinstance(InMemoryWebhookStore(), WebhookEventStore)
    assert isinstance(InMemoryTicketStore(), TicketReplayStore)


# ── webhooks ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_first_delivery_wins_then_duplicates(stores):
    hooks, _ = stores
    store = hooks()
    assert await store.record_if_new("acme", "evt-1") is True
    assert await store.record_if_new("acme", "evt-1") is False
    assert await store.record_if_new("other", "evt-1") is True  # keyed per provider
    assert await store.record_if_new("acme", "evt-2") is True


@pytest.mark.asyncio
async def test_webhook_forget_allows_a_retry(stores):
    hooks, _ = stores
    store = hooks()
    assert await store.record_if_new("acme", "evt-1")
    await store.forget("acme", "evt-1")
    assert await store.record_if_new("acme", "evt-1") is True
    await store.forget("acme", "never-existed")  # no error


@pytest.mark.asyncio
async def test_webhook_entries_expire(stores):
    hooks, _ = stores
    store = hooks(ttl=1)
    assert await store.record_if_new("acme", "evt-1")
    assert await store.record_if_new("acme", "evt-1") is False
    state = {"reclaimed": False}

    async def reclaimed():
        state["reclaimed"] = await store.record_if_new("acme", "evt-1")

    deadline = time.monotonic() + 5
    while not state["reclaimed"] and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        await reclaimed()
    assert state["reclaimed"], "expired idempotency key was never re-claimable"


@pytest.mark.asyncio
async def test_webhook_concurrent_duplicates_have_exactly_one_winner(stores):
    hooks, _ = stores
    # alternate between two stores = two containers on the same backend
    # (for memory they are separate objects, so use one for the in-process claim)
    a, b = hooks(), hooks()
    targets = [a, b] if not isinstance(a, InMemoryWebhookStore) else [a, a]
    results = await asyncio.gather(*(targets[i % 2].record_if_new("acme", "race") for i in range(40)))
    assert results.count(True) == 1


# ── tickets ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ticket_is_consumed_once(stores):
    _, tickets = stores
    store = tickets()
    exp = time.time() + 30
    assert await store.consume("jti-1", exp) is True
    assert await store.consume("jti-1", exp) is False
    assert await store.consume("jti-2", exp) is True


@pytest.mark.asyncio
async def test_ticket_concurrent_use_has_exactly_one_winner(stores):
    _, tickets = stores
    a, b = tickets(), tickets()
    targets = [a, b] if not isinstance(a, InMemoryTicketStore) else [a, a]
    exp = time.time() + 30
    results = await asyncio.gather(*(targets[i % 2].consume("race", exp) for i in range(40)))
    assert results.count(True) == 1


@pytest.mark.asyncio
async def test_expired_tickets_are_forgotten_so_the_store_stays_small():
    now = [1000.0]
    store = InMemoryTicketStore(clock=lambda: now[0])
    await store.consume("old", 1010.0)
    now[0] = 1011.0
    await store.consume("new", 1100.0)
    assert set(store._used) == {"new"}


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_ticket_replay_is_blocked_across_containers():
    from src.adapters.events.postgres import PostgresTicketStore
    from src.adapters.persistence.postgres import create_pool

    admin = await fresh_pg_pool()
    p1, p2 = await create_pool(TEST_DATABASE_URL), await create_pool(TEST_DATABASE_URL)
    try:
        exp = time.time() + 30
        assert await PostgresTicketStore(p1).consume("shared", exp) is True
        assert await PostgresTicketStore(p2).consume("shared", exp) is False  # other container
    finally:
        for p in (p1, p2, admin):
            await p.close()
