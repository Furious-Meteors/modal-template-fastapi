"""Tests for the in-memory event bus, webhook store, and item-service publishing."""
import asyncio

import pytest

from src.adapters.events.memory import InMemoryEventBus, InMemoryWebhookStore
from src.adapters.persistence.memory import InMemoryItemRepository
from src.core.ports.event_bus import EventBus
from src.core.ports.webhook_store import WebhookEventStore
from src.core.services.item_service import ItemService


def test_adapters_satisfy_ports():
    assert isinstance(InMemoryEventBus(), EventBus)
    assert isinstance(InMemoryWebhookStore(), WebhookEventStore)


async def _next(agen, timeout=1.0):
    return await asyncio.wait_for(agen.__anext__(), timeout)


@pytest.mark.asyncio
async def test_publish_assigns_increasing_ids_and_ts():
    bus = InMemoryEventBus()
    a = await bus.publish("t", "x", {})
    b = await bus.publish("t", "x", {})
    assert int(b.id) > int(a.id)
    assert a.ts > 0


@pytest.mark.asyncio
async def test_subscriber_receives_only_events_published_after_subscribing():
    bus = InMemoryEventBus()
    await bus.publish("t", "old", {})
    sub = bus.subscribe("t")
    first = asyncio.ensure_future(_next(sub))
    await asyncio.sleep(0.01)
    await bus.publish("t", "new", {"n": 1})
    assert (await first).type == "new"
    await sub.aclose()


@pytest.mark.asyncio
async def test_topics_are_isolated():
    bus = InMemoryEventBus()
    sub = bus.subscribe("a")
    pending = asyncio.ensure_future(_next(sub, timeout=0.1))
    await asyncio.sleep(0.01)
    await bus.publish("b", "other", {})
    with pytest.raises(asyncio.TimeoutError):
        await pending
    await sub.aclose()


@pytest.mark.asyncio
async def test_replay_after_last_event_id_then_live_without_gaps_or_dupes():
    bus = InMemoryEventBus()
    e1 = await bus.publish("t", "e", {"n": 1})
    await bus.publish("t", "e", {"n": 2})
    await bus.publish("t", "e", {"n": 3})
    sub = bus.subscribe("t", last_event_id=e1.id)
    assert (await _next(sub)).data["n"] == 2
    assert (await _next(sub)).data["n"] == 3
    live = asyncio.ensure_future(_next(sub))
    await asyncio.sleep(0.01)
    await bus.publish("t", "e", {"n": 4})
    assert (await live).data["n"] == 4
    await sub.aclose()


@pytest.mark.asyncio
async def test_replay_buffer_is_bounded():
    bus = InMemoryEventBus(replay_size=2)
    first = await bus.publish("t", "e", {"n": 1})
    for n in (2, 3, 4):
        await bus.publish("t", "e", {"n": n})
    sub = bus.subscribe("t", last_event_id=first.id)
    assert [(await _next(sub)).data["n"] for _ in range(2)] == [3, 4]
    await sub.aclose()


@pytest.mark.asyncio
async def test_invalid_last_event_id_means_no_replay():
    bus = InMemoryEventBus()
    await bus.publish("t", "e", {})
    sub = bus.subscribe("t", last_event_id="not-a-number")
    with pytest.raises(asyncio.TimeoutError):
        await _next(sub, timeout=0.1)
    await sub.aclose()


@pytest.mark.asyncio
async def test_slow_consumer_is_disconnected_not_buffered_forever():
    bus = InMemoryEventBus(subscriber_queue_size=2)
    sub = bus.subscribe("t")
    first = asyncio.ensure_future(_next(sub))
    await asyncio.sleep(0.01)  # subscriber registered, then never drains
    for n in range(5):
        await bus.publish("t", "e", {"n": n})
    with pytest.raises(StopAsyncIteration):
        await first
        await _next(sub)
    # Publishing afterwards must not raise or grow the dead subscriber's queue.
    await bus.publish("t", "e", {})


@pytest.mark.asyncio
async def test_unsubscribing_removes_subscriber():
    bus = InMemoryEventBus()
    sub = bus.subscribe("t")
    task = asyncio.ensure_future(_next(sub))
    await asyncio.sleep(0.01)
    assert len(bus._subscribers["t"]) == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await sub.aclose()
    assert len(bus._subscribers["t"]) == 0


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


@pytest.mark.asyncio
async def test_item_service_publishes_events():
    bus = InMemoryEventBus()
    service = ItemService(InMemoryItemRepository(), bus)
    sub = bus.subscribe("items")
    got: list = []

    async def collect():
        async for e in sub:
            got.append(e)

    task = asyncio.ensure_future(collect())
    await asyncio.sleep(0.01)
    item = await service.create("Widget")
    await service.update(item.id, "Widget 2")
    await service.delete(item.id)
    await service.delete(item.id)  # already gone: no event
    await asyncio.sleep(0.05)
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
