"""
One behavioural contract, run against every ItemRepository implementation.

The in-memory adapter always runs. The Postgres adapter runs when
TEST_DATABASE_URL points at a disposable database, e.g.

    docker run -d -e POSTGRES_PASSWORD=pgpass -e POSTGRES_DB=app -p 55432:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql://postgres:pgpass@localhost:55432/app pytest

Tables are dropped and recreated per test — never point this at real data.
"""
import asyncio
import os

import pytest
import pytest_asyncio

from src.adapters.persistence.memory import InMemoryItemRepository
from src.core.domain.errors import DuplicateItemNameError
from src.core.domain.item import Item
from src.core.ports.item_repository import ItemRepository
from src.core.services.item_service import ItemService

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

BACKENDS = [
    "memory",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set"),
    ),
]


@pytest_asyncio.fixture(params=BACKENDS)
async def repo(request):
    if request.param == "memory":
        yield InMemoryItemRepository()
        return
    from src.adapters.persistence.postgres import (
        PostgresItemRepository,
        create_pool,
        ensure_schema,
    )

    pool = await create_pool(TEST_DATABASE_URL)
    await pool.execute("DROP TABLE IF EXISTS items")
    await ensure_schema(pool)
    try:
        yield PostgresItemRepository(pool)
    finally:
        await pool.close()


def _item(i: int, name: str | None = None, **kw) -> Item:
    return Item(id=f"id-{i}", name=name or f"item {i}", **kw)


@pytest.mark.asyncio
async def test_satisfies_port(repo):
    assert isinstance(repo, ItemRepository)


@pytest.mark.asyncio
async def test_create_get_roundtrip_including_nulls(repo):
    await repo.create(_item(1, description="d", project_id="p"))
    await repo.create(_item(2))
    full, bare = await repo.get("id-1"), await repo.get("id-2")
    assert (full.name, full.description, full.project_id) == ("item 1", "d", "p")
    assert (bare.description, bare.project_id) == (None, None)


@pytest.mark.asyncio
async def test_get_missing_returns_none_for_any_string_id(repo):
    assert await repo.get("ghost-id") is None
    assert await repo.get("not-a-uuid-at-all") is None


@pytest.mark.asyncio
async def test_update_replaces_fields_and_missing_returns_none(repo):
    await repo.create(_item(1, description="old", project_id="p"))
    updated = await repo.update(Item(id="id-1", name="renamed"))
    assert (updated.name, updated.description, updated.project_id) == ("renamed", None, None)
    assert (await repo.get("id-1")).name == "renamed"
    assert await repo.update(_item(99)) is None


@pytest.mark.asyncio
async def test_delete(repo):
    await repo.create(_item(1))
    assert await repo.delete("id-1") is True
    assert await repo.delete("id-1") is False
    assert await repo.get("id-1") is None


@pytest.mark.asyncio
async def test_list_is_insertion_ordered_and_paginated(repo):
    for i in range(5):
        await repo.create(_item(i))
    page, total = await repo.list(limit=2, offset=0)
    assert [x.id for x in page] == ["id-0", "id-1"] and total == 5
    page, total = await repo.list(limit=2, offset=3)
    assert [x.id for x in page] == ["id-3", "id-4"] and total == 5
    page, total = await repo.list(limit=10, offset=50)
    assert page == [] and total == 5


@pytest.mark.asyncio
async def test_list_keeps_order_after_update_and_delete(repo):
    for i in range(3):
        await repo.create(_item(i))
    await repo.update(Item(id="id-0", name="changed"))
    await repo.delete("id-1")
    page, total = await repo.list(limit=10, offset=0)
    assert [x.id for x in page] == ["id-0", "id-2"] and total == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["widget", "  WIDGET  ", "Widget"])
async def test_get_by_name_ignores_case_and_surrounding_whitespace(repo, probe):
    await repo.create(_item(1, "Widget"))
    assert (await repo.get_by_name(probe)).id == "id-1"
    assert await repo.get_by_name("gadget") is None


@pytest.mark.asyncio
async def test_non_ascii_names(repo):
    await repo.create(_item(1, "Äpfel 🍎"))
    assert (await repo.get_by_name("äPFEL 🍎")).id == "id-1"


# ── Service-level uniqueness, including the race the pre-check cannot close ──

@pytest.mark.asyncio
async def test_service_rejects_duplicate_names(repo):
    service = ItemService(repo)
    await service.create("Widget")
    with pytest.raises(DuplicateItemNameError):
        await service.create("  widget ")


@pytest.mark.asyncio
async def test_service_rename_clash_but_keeping_own_name_is_fine(repo):
    service = ItemService(repo)
    a, b = await service.create("A"), await service.create("B")
    with pytest.raises(DuplicateItemNameError):
        await service.update(b.id, "a")
    assert (await service.update(a.id, "A")).name == "A"


@pytest.mark.asyncio
async def test_concurrent_creates_with_same_name_yield_exactly_one_winner(repo):
    service = ItemService(repo)
    results = await asyncio.gather(
        *(service.create("Same Name") for _ in range(12)), return_exceptions=True
    )
    winners = [r for r in results if isinstance(r, Item)]
    losers = [r for r in results if isinstance(r, DuplicateItemNameError)]
    assert len(winners) == 1 and len(losers) == 11
    assert (await repo.list(limit=50, offset=0))[1] == 1


@pytest.mark.asyncio
async def test_concurrent_renames_to_same_name_yield_exactly_one_winner(repo):
    service = ItemService(repo)
    items = [await service.create(f"n{i}") for i in range(8)]
    results = await asyncio.gather(
        *(service.update(it.id, "taken") for it in items), return_exceptions=True
    )
    assert sum(isinstance(r, Item) for r in results) == 1
    assert sum(isinstance(r, DuplicateItemNameError) for r in results) == 7
