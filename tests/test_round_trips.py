"""
Round-trip budget. Every call to the database costs a network round trip, and the cost
shows: at a 30 ms round trip a 5-statement publish capped the whole system at ~6 writes/s.
These tests count calls against a real database and fail if a hot path gets slower.
(asyncpg without a statement cache — required behind a transaction pooler — makes each
parametrised call two wire round trips; the budget counts calls, so it is relative.)
"""
import pytest
import pytest_asyncio

from src.core.domain.item import Item
from src.core.services.item_service import ItemService
from tests.conftest import fresh_pg_pool, requires_postgres

pytestmark = requires_postgres


class CountingPool:
    """Wraps an asyncpg pool; counts calls that hit the database and any connection checkouts."""

    def __init__(self, pool):
        self._pool = pool
        self.calls = 0
        self.acquires = 0

    def __getattr__(self, name):
        return getattr(self._pool, name)

    def _count(self, name):
        async def run(*a, **k):
            self.calls += 1
            return await getattr(self._pool, name)(*a, **k)
        return run

    def __getattribute__(self, name):
        if name in ("fetch", "fetchrow", "fetchval", "execute"):
            return object.__getattribute__(self, "_count")(name)
        return object.__getattribute__(self, name)

    def acquire(self, *a, **k):  # a checkout means a hand-rolled transaction: several round trips
        self.acquires += 1
        return self._pool.acquire(*a, **k)

    def reset(self):
        self.calls = self.acquires = 0


@pytest_asyncio.fixture
async def counted():
    from src.adapters.events.postgres import PostgresEventBus
    from src.adapters.persistence.postgres import PostgresItemRepository

    real = await fresh_pg_pool()
    pool = CountingPool(real)
    repo = PostgresItemRepository(pool)
    bus = PostgresEventBus(pool)  # not started: publish() only
    yield pool, repo, bus, ItemService(repo, bus)
    await real.close()


@pytest.mark.asyncio
async def test_list_is_one_database_call_with_no_transaction(counted):
    pool, repo, _, _ = counted
    for i in range(3):
        await repo.create(Item(id=f"i{i}", name=f"n{i}"))
    pool.reset()
    page, total = await repo.list(limit=2, offset=0)
    assert (len(page), total) == (2, 3)
    assert (pool.calls, pool.acquires) == (1, 0)


@pytest.mark.asyncio
async def test_listing_an_empty_table_or_a_page_past_the_end_costs_at_most_two_calls(counted):
    pool, repo, _, _ = counted
    pool.reset()
    assert await repo.list(limit=10, offset=0) == ([], 0)
    assert pool.calls == 1 and pool.acquires == 0
    await repo.create(Item(id="a", name="a"))
    pool.reset()
    assert await repo.list(limit=10, offset=9) == ([], 1)
    assert pool.calls == 2 and pool.acquires == 0


@pytest.mark.asyncio
async def test_each_repository_operation_is_one_call(counted):
    pool, repo, _, _ = counted
    for name, op in (
        ("create", lambda: repo.create(Item(id="x", name="x"))),
        ("get", lambda: repo.get("x")),
        ("get_by_name", lambda: repo.get_by_name("x")),
        ("update", lambda: repo.update(Item(id="x", name="y"))),
        ("delete", lambda: repo.delete("x")),
    ):
        pool.reset()
        await op()
        assert (pool.calls, pool.acquires) == (1, 0), f"{name}: {pool.calls} calls, {pool.acquires} checkouts"


@pytest.mark.asyncio
async def test_publishing_an_event_is_one_call_with_no_transaction(counted):
    pool, _, bus, _ = counted
    pool.reset()
    event = await bus.publish("t", "e", {"k": "v"})
    assert event.data == {"k": "v"}
    assert (pool.calls, pool.acquires) == (1, 0)


@pytest.mark.asyncio
async def test_a_service_write_is_the_write_plus_one_publish_and_nothing_else(counted):
    """create/update/delete used to cost ~7/~8/~6 calls (pre-checks and a 5-step publish)."""
    pool, _, _, service = counted
    pool.reset()
    item = await service.create("Widget")
    assert (pool.calls, pool.acquires) == (2, 0)          # INSERT + publish_event()
    pool.reset()
    await service.update(item.id, "Widget 2")
    assert (pool.calls, pool.acquires) == (2, 0)          # UPDATE + publish_event()
    pool.reset()
    assert await service.delete(item.id) is True
    assert (pool.calls, pool.acquires) == (2, 0)          # DELETE + publish_event()
    pool.reset()
    assert await service.delete(item.id) is False         # nothing deleted, so nothing published
    assert (pool.calls, pool.acquires) == (1, 0)


@pytest.mark.asyncio
async def test_service_reads_are_one_call(counted):
    pool, _, _, service = counted
    item = await service.create("Readable")
    pool.reset()
    await service.get(item.id)
    assert pool.calls == 1
    pool.reset()
    await service.list(limit=5, offset=0)
    assert pool.calls == 1
    pool.reset()
    assert await service.update("ghost", "x") is None     # a missing item: one failed UPDATE, no publish
    assert pool.calls == 1
