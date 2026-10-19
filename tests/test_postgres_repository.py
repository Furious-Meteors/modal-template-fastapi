"""Postgres-specific behaviour (skipped unless TEST_DATABASE_URL is set)."""
import asyncio
import os

import pytest
import pytest_asyncio

from src.core.domain.errors import DuplicateItemNameError
from src.core.domain.item import Item

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest_asyncio.fixture
async def pool():
    from src.adapters.persistence.postgres import create_pool, ensure_schema

    p = await create_pool(TEST_DATABASE_URL)
    await p.execute("DROP TABLE IF EXISTS items")
    await ensure_schema(p)
    yield p
    await p.close()


@pytest.fixture
def repo(pool):
    from src.adapters.persistence.postgres import PostgresItemRepository

    return PostgresItemRepository(pool)


@requires_db
@pytest.mark.asyncio
async def test_unique_index_raises_domain_error_directly_on_create(repo):
    await repo.create(Item(id="a", name="Widget"))
    with pytest.raises(DuplicateItemNameError):
        await repo.create(Item(id="b", name="  WIDGET "))  # bypasses the service pre-check
    assert (await repo.list(10, 0))[1] == 1


@requires_db
@pytest.mark.asyncio
async def test_unique_index_raises_domain_error_directly_on_update(repo):
    await repo.create(Item(id="a", name="A"))
    await repo.create(Item(id="b", name="B"))
    with pytest.raises(DuplicateItemNameError):
        await repo.update(Item(id="b", name="a "))
    assert (await repo.get("b")).name == "B"  # failed update changed nothing


@requires_db
@pytest.mark.asyncio
async def test_duplicate_id_is_not_misreported_as_duplicate_name(repo):
    import asyncpg

    await repo.create(Item(id="a", name="A"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.create(Item(id="a", name="Different"))


@requires_db
@pytest.mark.asyncio
async def test_ensure_schema_is_idempotent_and_safe_to_run_concurrently(pool):
    from src.adapters.persistence.postgres import ensure_schema

    await asyncio.gather(*(ensure_schema(pool) for _ in range(6)))
    await ensure_schema(pool)


@requires_db
@pytest.mark.asyncio
async def test_row_level_security_is_enabled(pool):
    enabled = await pool.fetchval("SELECT relrowsecurity FROM pg_class WHERE relname = 'items'")
    assert enabled is True


@requires_db
@pytest.mark.asyncio
async def test_list_total_and_page_come_from_one_snapshot(repo, pool):
    for i in range(5):
        await repo.create(Item(id=f"id-{i}", name=f"n{i}"))
    page, total = await repo.list(limit=2, offset=0)
    assert len(page) == 2 and total == 5


@requires_db
@pytest.mark.asyncio
async def test_ping(repo):
    await repo.ping()


def test_ssl_defaults():
    from src.adapters.persistence.postgres import _ssl_setting

    assert _ssl_setting("postgresql://u:p@localhost:5432/db") is None
    assert _ssl_setting("postgresql://u:p@127.0.0.1/db") is None
    assert _ssl_setting("postgresql://u:p@db.supabase.co:6543/postgres") == "require"
    assert _ssl_setting("postgresql://u:p@db.supabase.co/postgres?sslmode=verify-full") is None


def test_ssl_env_override(monkeypatch):
    from src.adapters.persistence.postgres import _ssl_setting

    monkeypatch.setenv("DATABASE_SSL", "disable")
    assert _ssl_setting("postgresql://u:p@db.supabase.co/postgres") is None
    monkeypatch.setenv("DATABASE_SSL", "verify-full")
    assert _ssl_setting("postgresql://u:p@localhost/db") == "verify-full"


# ── Outage translation (no database needed) ─────────────────────────────────

class _DeadPool:
    def __init__(self, exc):
        self._exc = exc

    async def fetchrow(self, *a, **k):
        raise self._exc

    fetchval = execute = fetch = fetchrow

    def acquire(self):
        raise self._exc


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_name", [
    "CannotConnectNowError", "AdminShutdownError", "TooManyConnectionsError",
    "ConnectionDoesNotExistError", "InterfaceError",
])
async def test_connection_failures_become_storage_unavailable(exc_name):
    import asyncpg

    from src.adapters.persistence.postgres import PostgresItemRepository
    from src.core.domain.errors import StorageUnavailableError

    repo = PostgresItemRepository(_DeadPool(getattr(asyncpg, exc_name)("down")))
    for call in (repo.get("x"), repo.list(10, 0), repo.delete("x"), repo.ping()):
        with pytest.raises(StorageUnavailableError):
            await call


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [ConnectionRefusedError("refused"), TimeoutError("slow")])
async def test_os_and_timeout_errors_become_storage_unavailable(exc):
    from src.adapters.persistence.postgres import PostgresItemRepository
    from src.core.domain.errors import StorageUnavailableError

    with pytest.raises(StorageUnavailableError):
        await PostgresItemRepository(_DeadPool(exc)).get("x")


@pytest.mark.asyncio
async def test_real_bugs_are_not_masked_as_unavailability():
    import asyncpg

    from src.adapters.persistence.postgres import PostgresItemRepository

    with pytest.raises(asyncpg.PostgresSyntaxError):
        await PostgresItemRepository(_DeadPool(asyncpg.PostgresSyntaxError("bad sql"))).get("x")


@pytest.mark.asyncio
async def test_pool_disables_the_prepared_statement_cache(monkeypatch):
    """Required behind transaction-mode poolers (verified against PgBouncer 1.20)."""
    import asyncpg

    from src.adapters.persistence import postgres

    captured: dict = {}

    async def fake_create_pool(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    await postgres.create_pool("postgresql://u:p@db.example.com:6543/postgres")
    assert captured["statement_cache_size"] == 0
    assert captured["ssl"] == "require"
