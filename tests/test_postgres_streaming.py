"""
Postgres-backed streaming state across simulated containers.

Each "container" is its own PostgresEventBus on its own connection pool, which is
what two Modal containers look like to the database. Skipped without
TEST_DATABASE_URL (a disposable database — tables are dropped).
"""
import asyncio
import time

import pytest
import pytest_asyncio

from src.adapters.events.postgres import PostgresEventBus
from tests.asgi_helpers import wait_until
from tests.conftest import LISTEN_URL, TEST_DATABASE_URL, fresh_pg_pool, requires_listen, requires_postgres

pytestmark = requires_postgres


@pytest_asyncio.fixture
async def cluster():
    """Factory for extra 'containers'. The first pool owns the (reset) schema."""
    from src.adapters.persistence.postgres import create_pool

    pools = [await fresh_pg_pool()]
    buses: list[PostgresEventBus] = []

    class Cluster:
        admin = pools[0]

        async def container(self, **kw) -> PostgresEventBus:
            pool = await create_pool(TEST_DATABASE_URL)
            pools.append(pool)
            kw.setdefault("poll_interval", 0.02)
            bus = PostgresEventBus(pool, **kw)
            await bus.start()
            buses.append(bus)
            return bus

    yield Cluster()
    for bus in buses:
        await bus.close()
    for pool in pools:
        await pool.close()


async def _next(agen, timeout=5.0):
    return await asyncio.wait_for(agen.__anext__(), timeout)


@pytest.mark.asyncio
async def test_event_published_on_one_container_reaches_a_stream_on_another(cluster):
    a, b = await cluster.container(), await cluster.container()
    sub = b.subscribe("items")
    first = asyncio.ensure_future(_next(sub))
    await wait_until(lambda: b.subscriber_count("items") == 1)
    sent = await a.publish("items", "item.created", {"id": "x"})
    got = await first
    assert (got.id, got.type, got.data) == (sent.id, "item.created", {"id": "x"})
    await sub.aclose()


@pytest.mark.asyncio
async def test_replay_crosses_containers(cluster):
    a, b = await cluster.container(), await cluster.container()
    e1 = await a.publish("t", "e", {"n": 1})
    await a.publish("t", "e", {"n": 2})
    sub = b.subscribe("t", last_event_id=e1.id)  # B never saw these live
    assert (await _next(sub)).data["n"] == 2
    await sub.aclose()


@pytest.mark.asyncio
async def test_new_container_does_not_replay_history_unasked(cluster):
    a = await cluster.container()
    await a.publish("t", "old", {})
    b = await cluster.container()  # starts at the head of the log
    sub = b.subscribe("t")
    with pytest.raises(asyncio.TimeoutError):
        await _next(sub, timeout=0.4)
    await sub.aclose()


@pytest.mark.asyncio
async def test_concurrent_publishers_on_many_containers_are_delivered_in_order_without_gaps(cluster):
    """The commit-order guarantee: a lower seq can never surface after a higher one."""
    pubs = [await cluster.container() for _ in range(3)]
    reader = await cluster.container()
    expected = 3 * 4 * 25  # 3 containers x 4 concurrent publishers x 25 events
    seen: list = []

    async def consume():
        async for e in reader.subscribe("t"):
            seen.append(e)
            if len(seen) == expected:
                return

    consumer = asyncio.ensure_future(consume())
    await wait_until(lambda: reader.subscriber_count("t") == 1)

    async def publisher(bus, pid):
        for n in range(25):
            await bus.publish("t", "e", {"p": pid, "n": n})

    await asyncio.gather(*(publisher(pubs[i % 3], i) for i in range(12)))
    await asyncio.wait_for(consumer, 20)

    seqs = [int(e.id) for e in seen]
    assert seqs == sorted(seqs) and len(set(seqs)) == expected  # strictly increasing, no dupes
    db_seqs = [r["seq"] for r in await cluster.admin.fetch("SELECT seq FROM events ORDER BY seq")]
    assert seqs == db_seqs  # and no gaps: exactly what the table holds
    for pid in range(12):  # each publisher's own order is preserved
        assert [e.data["n"] for e in seen if e.data["p"] == pid] == list(range(25))


@requires_listen
@pytest.mark.asyncio
async def test_listen_notify_wakes_the_pump_immediately(cluster):
    # Poll interval of 30s: only NOTIFY can explain a sub-second delivery.
    b = await cluster.container(listen_dsn=LISTEN_URL, poll_interval=30)
    a = await cluster.container(poll_interval=30)
    await wait_until(lambda: b._listening)
    sub = b.subscribe("t")
    first = asyncio.ensure_future(_next(sub))
    await wait_until(lambda: b.subscriber_count("t") == 1)
    t0 = time.monotonic()
    await a.publish("t", "e", {})
    await first
    assert time.monotonic() - t0 < 2.0
    await sub.aclose()


@pytest.mark.asyncio
async def test_polling_works_without_a_listen_connection(cluster):
    b = await cluster.container(poll_interval=0.05)  # no listen_dsn
    assert b._listening is False
    a = await cluster.container()
    sub = b.subscribe("t")
    first = asyncio.ensure_future(_next(sub))
    await wait_until(lambda: b.subscriber_count("t") == 1)
    await a.publish("t", "e", {})
    await first
    await sub.aclose()


@requires_listen
@pytest.mark.asyncio
async def test_listener_loss_falls_back_to_polling_and_then_reconnects(cluster):
    b = await cluster.container(listen_dsn=LISTEN_URL, poll_interval=0.05)
    a = await cluster.container()
    await wait_until(lambda: b._listening)
    await cluster.admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query ILIKE 'LISTEN%'"
    )
    await wait_until(lambda: not b._listening)
    sub = b.subscribe("t")
    first = asyncio.ensure_future(_next(sub))
    await wait_until(lambda: b.subscriber_count("t") == 1)
    await a.publish("t", "e", {"during": "outage"})
    assert (await first).data == {"during": "outage"}  # delivered by polling
    await wait_until(lambda: b._listening, timeout=10)  # and LISTEN came back on its own
    await sub.aclose()


@pytest.mark.asyncio
async def test_prune_drops_old_rows_and_caps_the_table(cluster):
    bus = await cluster.container(retention_seconds=3600, max_rows=5)
    for n in range(12):
        await bus.publish("t", "e", {"n": n})
    await bus._prune()
    rows = await cluster.admin.fetch("SELECT data::text AS d FROM events ORDER BY seq")
    assert len(rows) == 5 and '"n": 11' in rows[-1]["d"]  # newest kept

    await cluster.admin.execute("UPDATE events SET ts = now() - interval '2 hours'")
    await bus._prune()
    assert await cluster.admin.fetchval("SELECT count(*) FROM events") == 0


@requires_listen
@pytest.mark.asyncio
async def test_close_ends_subscriber_streams_and_stops_tasks(cluster):
    bus = await cluster.container(listen_dsn=LISTEN_URL)
    sub = bus.subscribe("t")
    pending = asyncio.ensure_future(_next(sub))
    await wait_until(lambda: bus.subscriber_count("t") == 1)
    await bus.close()
    with pytest.raises(StopAsyncIteration):
        await pending
        await _next(sub)
    assert all(t.done() for t in bus._tasks) or bus._tasks == []


@pytest.mark.asyncio
async def test_start_is_idempotent(cluster):
    bus = await cluster.container()
    tasks = list(bus._tasks)
    await bus.start()
    assert bus._tasks == tasks


@pytest.mark.asyncio
async def test_publish_failure_is_storage_unavailable_not_a_crash(cluster):
    from src.core.domain.errors import StorageUnavailableError

    bus = await cluster.container()
    await bus._pool.close()
    with pytest.raises(StorageUnavailableError):
        await bus.publish("t", "e", {})


@pytest.mark.asyncio
async def test_a_single_large_batch_does_not_drop_a_healthy_consumer(cluster):
    """300 rows land in ONE pump batch (inserted in one statement); queue holds 16."""
    bus = await cluster.container(subscriber_queue_size=16, poll_interval=30)
    got: list[int] = []

    async def consume():
        async for e in bus.subscribe("t"):
            got.append(int(e.data["n"]))

    task = asyncio.ensure_future(consume())
    await wait_until(lambda: bus.subscriber_count("t") == 1)
    await cluster.admin.execute(
        "INSERT INTO events (topic, type, data) "
        "SELECT 't', 'e', jsonb_build_object('n', g) FROM generate_series(1, 300) g"
    )
    bus._wake.set()  # one drain sees all 300 rows at once
    await wait_until(lambda: len(got) == 300, timeout=10)
    assert got == list(range(1, 301))
    assert bus.subscriber_count("t") == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_genuinely_stalled_consumer_is_still_dropped(cluster):
    bus = await cluster.container(subscriber_queue_size=8, poll_interval=30)
    sub = bus.subscribe("t")
    first = asyncio.ensure_future(_next(sub))  # takes one event, then nobody reads
    await wait_until(lambda: bus.subscriber_count("t") == 1)
    await cluster.admin.execute(
        "INSERT INTO events (topic, type, data) "
        "SELECT 't', 'e', jsonb_build_object('n', g) FROM generate_series(1, 200) g"
    )
    bus._wake.set()
    await wait_until(lambda: bus.subscriber_count("t") == 0, timeout=10)
    await first


@pytest.mark.asyncio
async def test_a_listen_connection_that_never_hears_notifications_is_detected(cluster, caplog):
    """
    A LISTEN connection to the wrong database (or a transaction-mode pooler) connects
    fine and then never receives anything. The bus must notice, say so, and keep
    delivering by polling instead of silently running on the slow safety poll.
    """
    import logging
    from urllib.parse import urlsplit

    from tests.conftest import is_local

    if not is_local(TEST_DATABASE_URL):
        pytest.skip("creates a database; only run against a local Postgres")
    other = "listen_probe_other"
    await cluster.admin.execute(f'DROP DATABASE IF EXISTS "{other}"')
    await cluster.admin.execute(f'CREATE DATABASE "{other}"')
    try:
        parts = urlsplit(TEST_DATABASE_URL)
        deaf_dsn = parts._replace(path=f"/{other}").geturl()
        with caplog.at_level(logging.WARNING, logger="src.adapters.events.postgres"):
            bus = await cluster.container(listen_dsn=deaf_dsn, listen_probe_timeout=0.4, poll_interval=0.05)
            await wait_until(lambda: any("did not receive a test NOTIFY" in r.getMessage() for r in caplog.records), timeout=10)
        assert bus._listening is False  # not trusted

        sub = bus.subscribe("t")
        first = asyncio.ensure_future(_next(sub))
        await wait_until(lambda: bus.subscriber_count("t") == 1)
        publisher = await cluster.container()
        t0 = time.monotonic()
        await publisher.publish("t", "e", {})
        await first
        assert time.monotonic() - t0 < 1.5  # fast polling, not the 5s safety net
        await sub.aclose()
    finally:
        await cluster.admin.execute(f'DROP DATABASE IF EXISTS "{other}" WITH (FORCE)')
