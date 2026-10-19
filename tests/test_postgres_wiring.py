"""
The real app lifespan on Postgres: init_resources -> pool -> schema -> bus pump,
through the telemetry sidecars, and a clean shutdown. Skipped without
TEST_DATABASE_URL (a disposable database — tables are dropped).
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from src import deps
from src.adapters.telemetry.metered import MeteredEventBus, MeteredItemService
from src.api.auth import get_current_user
from src.api.models import TokenPayload
from tests.conftest import TEST_DATABASE_URL, fresh_pg_pool, requires_postgres

pytestmark = requires_postgres


def _reset_schema():
    async def run():
        pool = await fresh_pg_pool()
        await pool.close()

    asyncio.run(run())


def _query(sql: str):
    async def run():
        from src.adapters.persistence.postgres import create_pool

        pool = await create_pool(TEST_DATABASE_URL)
        try:
            return await pool.fetch(sql)
        finally:
            await pool.close()

    return asyncio.run(run())


@pytest.fixture
def pg_app(monkeypatch):
    from src.main import app

    _reset_schema()
    monkeypatch.setenv("PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("EVENT_BUS_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    for cached in (deps._get_repository, deps._get_event_bus, deps._get_webhook_store, deps._get_ticket_store):
        cached.cache_clear()
    # Use the REAL dependencies: drop the overrides the autouse fixtures installed.
    for dep in (deps.get_item_service, deps.get_event_bus, deps.get_webhook_store, deps.get_ticket_store):
        app.dependency_overrides.pop(dep, None)
    app.dependency_overrides[get_current_user] = lambda: TokenPayload(
        sub="u", exp=9_999_999_999, scopes=["items:read", "items:write", "events:read"]
    )
    yield app
    app.dependency_overrides.pop(get_current_user, None)
    for cached in (deps._get_repository, deps._get_event_bus, deps._get_webhook_store, deps._get_ticket_store):
        cached.cache_clear()


def test_lifespan_wires_postgres_through_the_sidecars_and_shuts_down_cleanly(pg_app):
    with TestClient(pg_app) as client:
        bus = deps._get_event_bus()
        assert isinstance(bus, MeteredEventBus)
        assert isinstance(deps.get_item_service(), MeteredItemService)
        assert len(bus._tasks) >= 1 and not any(t.done() for t in bus._tasks)  # pump is running

        assert client.get("/api/v1/health").json()["status"] == "healthy"
        created = client.post("/api/v1/items", json={"name": "wired"})
        assert created.status_code == 201
        assert client.post("/api/v1/items", json={"name": " WIRED"}).status_code == 409
        tasks = list(bus._tasks)
    assert all(t.done() for t in tasks)  # lifespan shutdown stopped the pump
    assert deps._pool is None

    assert [r["name"] for r in _query("SELECT name FROM items")] == ["wired"]
    events = _query("SELECT type FROM events ORDER BY seq")
    assert [r["type"] for r in events] == ["item.created"]  # one write, one event; the 409 published none


def test_restart_reuses_the_schema_and_keeps_data(pg_app):
    with TestClient(pg_app) as client:
        client.post("/api/v1/items", json={"name": "survivor"})
    for cached in (deps._get_repository, deps._get_event_bus, deps._get_webhook_store, deps._get_ticket_store):
        cached.cache_clear()
    with TestClient(pg_app) as client:  # second boot: schema bootstrap is idempotent
        names = [i["name"] for i in client.get("/api/v1/items").json()["items"]]
    assert names == ["survivor"]
