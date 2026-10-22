"""
Business metrics recorded by the telemetry sidecars (src/adapters/telemetry/metered.py).

Uses a private MeterProvider + in-memory reader per test: no global state, and it
checks exactly what an exporter would receive.
"""
import asyncio
import time

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from src.adapters.events.memory import InMemoryEventBus
from src.adapters.persistence.memory import InMemoryItemRepository
from src.adapters.telemetry.metered import MeteredEventBus, MeteredItemService
from src.core.domain.errors import DuplicateItemNameError, StorageUnavailableError
from src.core.domain.events import Event
from src.core.services.item_service import ItemService
from src.infrastructure.metrics import BusinessMetrics
from tests.asgi_helpers import wait_until


class Harness:
    def __init__(self):
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])
        self.metrics = BusinessMetrics(self.provider.get_meter("test"))

    def points(self, name: str) -> list:
        data = self.reader.get_metrics_data()
        out = []
        for rm in (data.resource_metrics if data else []):
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == name:
                        out.extend(m.data.data_points)
        return out

    def count(self, name: str, **labels) -> int:
        return sum(
            int(p.value) for p in self.points(name)
            if all(dict(p.attributes).get(k) == v for k, v in labels.items())
        )

    def hist(self, name: str, **labels):
        pts = [p for p in self.points(name)
               if all(dict(p.attributes).get(k) == v for k, v in labels.items())]
        return sum(p.count for p in pts), sum(p.sum for p in pts)


@pytest.fixture
def h():
    return Harness()


# ── BusinessMetrics ─────────────────────────────────────────────────────────

def test_item_op_records_counter_and_duration(h):
    h.metrics.record_item_op("create", "ok", 12.5)
    assert h.count("app.items.operations", operation="create", outcome="ok") == 1
    assert h.hist("app.items.operation.duration", operation="create", outcome="ok") == (1, 12.5)


def test_webhook_event_types_collapse_to_one_label_value(h):
    for provider in ("acme", "stripe", "github", "x" * 20):
        h.metrics.record_event_published("webhooks", f"webhook.{provider}", "ok", 1.0)
    h.metrics.record_event_published("items", "item.created", "ok", 1.0)
    types = {dict(p.attributes)["type"] for p in h.points("app.events.published")}
    assert types == {"webhook", "item.created"}  # bounded cardinality


def test_delivery_lag_is_clamped_and_replays_are_counted_not_timed(h):
    h.metrics.record_event_delivered("items", lag_ms=-250.0, replayed=False)  # clock skew
    h.metrics.record_event_delivered("items", lag_ms=40.0, replayed=False)
    h.metrics.record_event_delivered("items", lag_ms=999_999.0, replayed=True)
    assert h.hist("app.events.delivery.lag", topic="items") == (2, 40.0)  # -250 clamped to 0
    assert h.count("app.events.replayed", topic="items") == 1


def test_recording_never_raises_even_if_an_instrument_breaks(h):
    class Broken:
        def add(self, *a, **k): raise RuntimeError("exporter exploded")
        record = add

    h.metrics._item_ops = h.metrics._item_op_duration = Broken()
    h.metrics._events_published = h.metrics._publish_duration = Broken()
    h.metrics._delivery_lag = h.metrics._replayed = Broken()
    h.metrics.record_item_op("create", "ok", 1)
    h.metrics.record_event_published("items", "item.created", "ok", 1)
    h.metrics.record_event_delivered("items", 1, False)
    h.metrics.record_event_delivered("items", 1, True)


# ── MeteredItemService ──────────────────────────────────────────────────────

def _svc(h, **kw):
    return MeteredItemService(ItemService(InMemoryItemRepository(), **kw), h.metrics)


@pytest.mark.asyncio
async def test_item_operations_are_counted_with_the_right_outcome(h):
    svc = _svc(h)
    item = await svc.create("Widget")
    await svc.get(item.id)
    await svc.get("ghost")                      # not found
    await svc.list()
    await svc.update(item.id, "Widget 2")
    await svc.update("ghost", "x")              # not found
    assert await svc.delete(item.id) is True
    assert await svc.delete(item.id) is False   # already gone

    c = lambda op, out: h.count("app.items.operations", operation=op, outcome=out)
    assert (c("create", "ok"), c("list", "ok")) == (1, 1)
    assert (c("get", "ok"), c("get", "not_found")) == (1, 1)
    assert (c("update", "ok"), c("update", "not_found")) == (1, 1)
    assert (c("delete", "ok"), c("delete", "not_found")) == (1, 1)


@pytest.mark.asyncio
async def test_the_sidecar_does_not_change_results_or_exceptions(h):
    svc = _svc(h)
    item = await svc.create("Same", description="d", project_id="p")
    assert (await svc.get(item.id)).description == "d"
    assert [i.id for i in (await svc.list())[0]] == [item.id]
    with pytest.raises(DuplicateItemNameError):
        await svc.create(" same ")
    assert h.count("app.items.operations", operation="create", outcome="conflict") == 1


@pytest.mark.asyncio
async def test_unavailable_and_unexpected_errors_are_classified_and_reraised(h):
    class Failing:
        def __init__(self, exc): self.exc = exc
        async def create(self, *a, **k): raise self.exc
        get = list = update = delete = create

    with pytest.raises(StorageUnavailableError):
        await MeteredItemService(Failing(StorageUnavailableError("down")), h.metrics).create("x")
    with pytest.raises(ZeroDivisionError):
        await MeteredItemService(Failing(ZeroDivisionError()), h.metrics).get("x")
    assert h.count("app.items.operations", operation="create", outcome="unavailable") == 1
    assert h.count("app.items.operations", operation="get", outcome="error") == 1


@pytest.mark.asyncio
async def test_duration_reflects_real_time_spent(h):
    class Slow:
        async def create(self, *a, **k):
            await asyncio.sleep(0.05)

    await MeteredItemService(Slow(), h.metrics).create("x")
    count, total = h.hist("app.items.operation.duration", operation="create")
    assert count == 1 and total >= 45  # ms


# ── MeteredEventBus ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_publish_is_counted_and_timed(h):
    bus = MeteredEventBus(InMemoryEventBus(), h.metrics)
    event = await bus.publish("items", "item.created", {"id": "1"})
    assert event.type == "item.created"  # result unchanged
    assert h.count("app.events.published", topic="items", type="item.created", outcome="ok") == 1
    assert h.hist("app.events.publish.duration", topic="items", outcome="ok")[0] == 1


@pytest.mark.asyncio
async def test_publish_failure_is_recorded_and_reraised(h):
    class Broken:
        async def publish(self, *a, **k): raise RuntimeError("bus down")

    with pytest.raises(RuntimeError, match="bus down"):
        await MeteredEventBus(Broken(), h.metrics).publish("items", "item.created", {})
    assert h.count("app.events.published", topic="items", outcome="error") == 1


@pytest.mark.asyncio
async def test_live_events_record_delivery_lag(h):
    bus = MeteredEventBus(InMemoryEventBus(), h.metrics)
    sub = bus.subscribe("items")
    first = asyncio.ensure_future(sub.__anext__())
    await wait_until(lambda: bus.subscriber_count("items") == 1)  # attribute passthrough
    await bus.publish("items", "item.created", {})
    event = await asyncio.wait_for(first, 2)
    assert event.type == "item.created"
    count, total = h.hist("app.events.delivery.lag", topic="items")
    assert count == 1 and 0 <= total < 1000
    assert h.count("app.events.replayed") == 0
    await sub.aclose()


@pytest.mark.asyncio
async def test_replayed_events_are_counted_separately_not_as_lag(h):
    old = time.time() - 3600

    class Stub:
        async def subscribe_gen(self):
            yield Event("1", "item.created", {}, old)      # an hour old: a replay
            yield Event("2", "item.created", {}, time.time())  # live

        def subscribe(self, topic, last_event_id=None):
            return self.subscribe_gen()

    bus = MeteredEventBus(Stub(), h.metrics)
    got = [e.id async for e in bus.subscribe("items")]
    assert got == ["1", "2"]
    assert h.count("app.events.replayed", topic="items") == 1
    assert h.hist("app.events.delivery.lag", topic="items")[0] == 1  # only the live one
    assert h.hist("app.events.delivery.lag", topic="items")[1] < 5000  # the hour did not pollute it


@pytest.mark.asyncio
async def test_closing_the_wrapper_closes_the_inner_subscription(h):
    state = {"closed": False}

    async def gen():
        try:
            while True:
                yield Event("1", "e", {}, time.time())
                await asyncio.sleep(0)
        finally:
            state["closed"] = True

    class Stub:
        def subscribe(self, topic, last_event_id=None):
            return gen()

    sub = MeteredEventBus(Stub(), h.metrics).subscribe("items")
    await sub.__anext__()
    await sub.aclose()
    assert state["closed"] is True  # no leaked subscriber behind the sidecar


@pytest.mark.asyncio
async def test_sidecar_works_with_the_heartbeat_wrapper_and_cancellation(h):
    """with_heartbeat cancels a pending __anext__ on shutdown; the sidecar must survive it."""
    from src.api import streaming

    bus = MeteredEventBus(InMemoryEventBus(), h.metrics)
    stream = streaming.with_heartbeat(bus.subscribe("items"), interval=0.02)
    assert await stream.__anext__() is None  # heartbeat while idle
    await bus.publish("items", "item.created", {})
    assert (await stream.__anext__()).type == "item.created"
    await stream.aclose()
    assert bus.subscriber_count("items") == 0


# ── wiring ──────────────────────────────────────────────────────────────────

def test_deps_wrap_the_service_and_bus_in_the_metrics_sidecar(monkeypatch):
    from src import deps

    monkeypatch.delenv("EVENT_BUS_BACKEND", raising=False)
    monkeypatch.delenv("PERSISTENCE_BACKEND", raising=False)
    deps._get_event_bus.cache_clear()
    deps._get_repository.cache_clear()
    try:
        assert isinstance(deps.get_event_bus(), MeteredEventBus)
        assert isinstance(deps.get_item_service(), MeteredItemService)
    finally:
        deps._get_event_bus.cache_clear()
        deps._get_repository.cache_clear()


def test_core_has_no_telemetry_or_adapter_imports():
    """The point of a sidecar: core/ must stay free of telemetry (and of adapters/infrastructure)."""
    import re
    from pathlib import Path

    core = Path(__file__).resolve().parent.parent / "src" / "core"
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:opentelemetry|src\.infrastructure|src\.adapters|src\.api)\b",
        re.MULTILINE,
    )
    offenders = [
        str(p.relative_to(core)) for p in core.rglob("*.py") if forbidden.search(p.read_text())
    ]
    assert offenders == []


# ── dropped subscriptions (slow consumers) ──────────────────────────────────

@pytest.mark.asyncio
async def test_a_consumer_the_bus_gave_up_on_is_counted_as_dropped(h):
    bus = MeteredEventBus(InMemoryEventBus(subscriber_queue_size=2), h.metrics)
    sub = bus.subscribe("items")
    first = asyncio.ensure_future(sub.__anext__())
    await wait_until(lambda: bus.subscriber_count("items") == 1)
    for n in range(8):                        # nobody keeps reading: the bus drops the laggard
        await bus.publish("items", "e", {"n": n})
    await first
    with pytest.raises(StopAsyncIteration):
        for _ in range(8):
            await sub.__anext__()
    assert h.count("app.events.subscriptions.dropped", topic="items", reason="ended_by_bus") == 1


@pytest.mark.asyncio
async def test_a_consumer_that_leaves_on_its_own_is_not_counted_as_dropped(h):
    bus = MeteredEventBus(InMemoryEventBus(), h.metrics)
    sub = bus.subscribe("items")
    first = asyncio.ensure_future(sub.__anext__())
    await wait_until(lambda: bus.subscriber_count("items") == 1)
    await bus.publish("items", "e", {})
    await first
    await sub.aclose()                        # client disconnect / max duration
    assert h.count("app.events.subscriptions.dropped") == 0
