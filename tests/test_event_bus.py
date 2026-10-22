"""
EventBus behavioural contract, run against every implementation.

The in-memory bus always runs; the Postgres bus runs when TEST_DATABASE_URL points
at a disposable database (see tests/conftest.py). Also covers the in-memory
webhook store and item-service publishing.
"""
import asyncio

import pytest
import pytest_asyncio

from src.adapters.events.memory import InMemoryEventBus, InMemoryWebhookStore
from src.adapters.persistence.memory import InMemoryItemRepository
from src.core.ports.event_bus import EventBus
from src.core.ports.webhook_store import WebhookEventStore
from src.core.services.item_service import ItemService
from tests.asgi_helpers import wait_until
from tests.conftest import BACKENDS, fresh_pg_pool


@pytest_asyncio.fixture(params=BACKENDS)
async def make_bus(request):
    """Factory for buses of the parametrised backend; all are closed afterwards."""
    created = []
    pool = None
    if request.param == "postgres":
        pool = await fresh_pg_pool()

    async def factory(**kwargs):
        if request.param == "memory":
            bus = InMemoryEventBus(**kwargs)
        else:
            from src.adapters.events.postgres import PostgresEventBus

            bus = PostgresEventBus(pool, poll_interval=0.02, **kwargs)
            await bus.start()
        created.append(bus)
        return bus

    yield factory
    for bus in created:
        if hasattr(bus, "close"):
            await bus.close()
    if pool is not None:
        await pool.close()


async def _next(agen, timeout=3.0):
    return await asyncio.wait_for(agen.__anext__(), timeout)


async def _subscribed(bus, topic="t", last_event_id=None):
    """Start a subscription and wait until it is actually registered."""
    sub = bus.subscribe(topic, last_event_id)
    first = asyncio.ensure_future(_next(sub))
    await wait_until(lambda: bus.subscriber_count(topic) >= 1)
    return sub, first


def test_memory_adapters_satisfy_ports():
    assert isinstance(InMemoryEventBus(), EventBus)
    assert isinstance(InMemoryWebhookStore(), WebhookEventStore)


@pytest.mark.asyncio
async def test_bus_satisfies_port(make_bus):
    assert isinstance(await make_bus(), EventBus)


@pytest.mark.asyncio
async def test_publish_assigns_increasing_ids_and_ts(make_bus):
    bus = await make_bus()
    a = await bus.publish("t", "x", {})
    b = await bus.publish("t", "x", {})
    assert int(b.id) > int(a.id)
    assert a.ts > 0


@pytest.mark.asyncio
async def test_data_roundtrips_exactly(make_bus):
    bus = await make_bus()
    payload = {"s": "ünï 🍎", "n": 1.5, "nested": {"a": [1, None, True]}, "empty": {}}
    sub, first = await _subscribed(bus)
    await bus.publish("t", "x", payload)
    assert (await first).data == payload
    await sub.aclose()


@pytest.mark.asyncio
async def test_subscriber_receives_only_events_published_after_subscribing(make_bus):
    bus = await make_bus()
    await bus.publish("t", "old", {})
    sub, first = await _subscribed(bus)
    await bus.publish("t", "new", {"n": 1})
    assert (await first).type == "new"
    await sub.aclose()


@pytest.mark.asyncio
async def test_topics_are_isolated(make_bus):
    bus = await make_bus()
    sub = bus.subscribe("a")
    pending = asyncio.ensure_future(_next(sub, timeout=0.3))
    await wait_until(lambda: bus.subscriber_count("a") == 1)
    await bus.publish("b", "other", {})
    with pytest.raises(asyncio.TimeoutError):
        await pending
    await sub.aclose()


@pytest.mark.asyncio
async def test_replay_after_last_event_id_then_live_without_gaps_or_dupes(make_bus):
    bus = await make_bus()
    e1 = await bus.publish("t", "e", {"n": 1})
    await bus.publish("t", "e", {"n": 2})
    await bus.publish("t", "e", {"n": 3})
    sub = bus.subscribe("t", last_event_id=e1.id)
    assert (await _next(sub)).data["n"] == 2
    assert (await _next(sub)).data["n"] == 3
    live = asyncio.ensure_future(_next(sub))
    await bus.publish("t", "e", {"n": 4})
    assert (await live).data["n"] == 4
    await sub.aclose()


@pytest.mark.asyncio
async def test_replay_buffer_is_bounded_to_the_most_recent_events(make_bus):
    bus = await make_bus(replay_size=2)
    first = await bus.publish("t", "e", {"n": 1})
    for n in (2, 3, 4):
        await bus.publish("t", "e", {"n": n})
    sub = bus.subscribe("t", last_event_id=first.id)
    assert [(await _next(sub)).data["n"] for _ in range(2)] == [3, 4]
    await sub.aclose()


@pytest.mark.asyncio
async def test_replay_is_scoped_to_the_topic(make_bus):
    bus = await make_bus()
    first = await bus.publish("t", "e", {"n": 1})
    await bus.publish("other", "e", {"n": "x"})
    await bus.publish("t", "e", {"n": 2})
    sub = bus.subscribe("t", last_event_id=first.id)
    assert (await _next(sub)).data["n"] == 2
    await sub.aclose()


@pytest.mark.asyncio
async def test_invalid_last_event_id_means_no_replay(make_bus):
    bus = await make_bus()
    await bus.publish("t", "e", {})
    sub = bus.subscribe("t", last_event_id="not-a-number")
    with pytest.raises(asyncio.TimeoutError):
        await _next(sub, timeout=0.3)
    await sub.aclose()


@pytest.mark.asyncio
async def test_slow_consumer_is_disconnected_not_buffered_forever(make_bus):
    bus = await make_bus(subscriber_queue_size=2)
    sub = bus.subscribe("t")
    first = asyncio.ensure_future(_next(sub))
    await wait_until(lambda: bus.subscriber_count("t") == 1)  # registered, then never drains
    for n in range(8):
        await bus.publish("t", "e", {"n": n})
    await wait_until(lambda: bus.subscriber_count("t") == 0)  # bus dropped the laggard
    with pytest.raises(StopAsyncIteration):
        await first
        await _next(sub)
    await bus.publish("t", "e", {})  # must neither raise nor grow the dead queue


@pytest.mark.asyncio
async def test_unsubscribing_removes_subscriber(make_bus):
    bus = await make_bus()
    sub, task = await _subscribed(bus)
    assert bus.subscriber_count("t") == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await sub.aclose()
    assert bus.subscriber_count("t") == 0


@pytest.mark.asyncio
async def test_many_subscribers_each_get_every_event_in_order(make_bus):
    bus = await make_bus()
    got = {i: [] for i in range(5)}

    async def consume(i):
        async for e in bus.subscribe("t"):
            got[i].append(e.data["n"])
            if len(got[i]) == 20:
                return

    tasks = [asyncio.ensure_future(consume(i)) for i in range(5)]
    await wait_until(lambda: bus.subscriber_count("t") == 5)
    for n in range(20):
        await bus.publish("t", "e", {"n": n})
    await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert all(v == list(range(20)) for v in got.values())


# ── webhook store (memory) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_store_detects_duplicates_and_forgets():
    store = InMemoryWebhookStore()
    assert await store.record_if_new("acme", "1") is True
    assert await store.record_if_new("acme", "1") is False
    assert await store.record_if_new("other", "1") is True  # keyed per provider
    await store.forget("acme", "1")
    assert await store.record_if_new("acme", "1") is True


@pytest.mark.asyncio
async def test_webhook_store_entries_expire_and_are_bounded():
    now = [0.0]
    store = InMemoryWebhookStore(ttl_seconds=10, max_entries=3, clock=lambda: now[0])
    assert await store.record_if_new("p", "a")
    now[0] = 11
    assert await store.record_if_new("p", "a")  # expired, so new again
    for i in range(10):
        await store.record_if_new("p", f"k{i}")
    assert len(store._seen) <= 4


# ── item service publishing ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_item_service_publishes_events(make_bus):
    bus = await make_bus()
    service = ItemService(InMemoryItemRepository(), bus)
    got: list = []

    async def collect():
        async for e in bus.subscribe("items"):
            got.append(e)

    task = asyncio.ensure_future(collect())
    await wait_until(lambda: bus.subscriber_count("items") == 1)
    item = await service.create("Widget")
    await service.update(item.id, "Widget 2")
    await service.delete(item.id)
    await service.delete(item.id)  # already gone: no event
    await wait_until(lambda: len(got) == 3)
    await asyncio.sleep(0.1)  # negative check: give a wrongly-published 4th event time to arrive
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [e.type for e in got] == ["item.created", "item.updated", "item.deleted"]
    assert got[2].data == {"id": item.id}


@pytest.mark.asyncio
async def test_bus_failure_does_not_fail_the_write():
    class BrokenBus:
        async def publish(self, *a, **k):
            raise RuntimeError("bus down")

        def subscribe(self, *a, **k):
            raise NotImplementedError

    service = ItemService(InMemoryItemRepository(), BrokenBus())
    item = await service.create("Still works")
    assert (await service.get(item.id)).name == "Still works"


# ── bursts: a healthy consumer must not be mistaken for a slow one ──────────

@pytest.mark.asyncio
async def test_healthy_consumer_survives_a_burst_far_larger_than_its_queue(make_bus):
    """Regression: dispatching a whole batch at once overflowed the queue and dropped healthy consumers."""
    bus = await make_bus(subscriber_queue_size=16)
    got: list[int] = []

    async def consume():
        async for e in bus.subscribe("t"):
            got.append(e.data["n"])

    task = asyncio.ensure_future(consume())
    await wait_until(lambda: bus.subscriber_count("t") == 1)
    for n in range(400):  # 25x the queue, published back to back
        await bus.publish("t", "e", {"n": n})
    await wait_until(lambda: len(got) == 400, timeout=15)
    assert got == list(range(400))
    assert bus.subscriber_count("t") == 1  # still subscribed, never dropped
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
