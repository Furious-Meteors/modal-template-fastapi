"""
src/adapters/persistence/postgres.py
====================================
Postgres persistence adapter (works with Supabase, Neon, RDS, plain Postgres).

Satisfies the ItemRepository Protocol structurally. Name uniqueness is enforced
by a unique index on lower(btrim(name)); a violation surfaces as
DuplicateItemNameError, so concurrent creates across containers cannot both win.

Connecting to Supabase
----------------------
Use the *pooler* connection string (Project Settings → Database → Connection
string → "Transaction pooler", port 6543) — Modal can run many containers and
direct connections (port 5432) would exhaust Postgres' connection limit.
Transaction-mode poolers hand each statement to whichever server connection is
free, so asyncpg's per-connection prepared-statement cache breaks on poolers
without prepared-statement support (Supavisor, PgBouncer < 1.21): verified with
PgBouncer 1.20, where the contract suite fails with
InvalidSQLStatementNameError ("prepared statement ... does not exist") unless
statement_cache_size=0. Modern PgBouncer tolerates the cache, so this costs a
little speed there and is required elsewhere. tests/test_postgres_repository.py
guards the setting.

DATABASE_MODE selects how the URL is reached: "pooler" (default; transaction pooler, statement
cache off) or "direct" (a direct or session-level connection: asyncpg's prepared-statement cache
stays on, so each parameterised query is ONE wire round trip instead of two, and LISTEN can use
the same URL). Use direct when the number of connections you need is within the database's limit.

Env vars: DATABASE_URL (required), DATABASE_MODE ("pooler" | "direct"), DATABASE_SSL ("require" default for remote
hosts, "disable" to turn off), DB_POOL_MIN_SIZE (1), DB_POOL_MAX_SIZE (5),
DB_COMMAND_TIMEOUT_SECONDS (10). Keep DB_POOL_MAX_SIZE >= the per-container
concurrency (max_concurrent_requests) so requests do not queue on the pool.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

import asyncpg

from src.core.domain.errors import DuplicateItemNameError, StorageUnavailableError
from src.core.domain.item import Item

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer (got {raw!r})") from None
    if value < 1:
        raise RuntimeError(f"{name} must be >= 1 (got {value})")
    return value


def _ssl_setting(dsn: str) -> Optional[str]:
    """'require' for remote hosts unless the DSN or DATABASE_SSL says otherwise."""
    override = os.environ.get("DATABASE_SSL", "").strip().lower()
    if override:
        return None if override in ("disable", "false", "0") else override
    parts = urlsplit(dsn)
    if "sslmode=" in (parts.query or ""):
        return None  # the DSN already decides; asyncpg parses sslmode itself
    return None if (parts.hostname or "") in _LOCAL_HOSTS else "require"


def database_mode() -> str:
    mode = os.environ.get("DATABASE_MODE", "pooler").strip().lower() or "pooler"
    if mode not in ("pooler", "direct"):
        raise RuntimeError(f"DATABASE_MODE must be 'pooler' or 'direct' (got {mode!r})")
    return mode


async def create_pool(dsn: str) -> asyncpg.Pool:
    max_size = _int_env("DB_POOL_MAX_SIZE", 5)
    min_size = min(_int_env("DB_POOL_MIN_SIZE", 1), max_size)
    kwargs: dict = dict(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=_int_env("DB_COMMAND_TIMEOUT_SECONDS", 10),
    )
    if database_mode() == "pooler":
        kwargs["statement_cache_size"] = 0  # safe behind any transaction-mode pooler (Supavisor)
    ssl = _ssl_setting(dsn)
    if ssl:
        kwargs["ssl"] = ssl
    return await asyncpg.create_pool(**kwargs)


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """Bring the schema up to date (versioned migrations; see migrations.py). Idempotent and concurrency-safe."""
    from src.adapters.persistence.migrations import run_migrations

    await run_migrations(pool)


# Connection-level failures: the database is unreachable, restarting, or out of
# connections — a retry later can succeed. Anything else is a real error.
_UNAVAILABLE = (
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError,
    asyncpg.CrashShutdownError,
    asyncpg.TooManyConnectionsError,
    asyncpg.InterfaceError,
    asyncio.TimeoutError,
    OSError,
)


def _translating(fn):
    """Turn connection-level failures into StorageUnavailableError (HTTP 503)."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except _UNAVAILABLE as exc:
            logger.warning("Postgres unavailable: %s: %s", type(exc).__name__, exc)
            raise StorageUnavailableError(type(exc).__name__) from exc

    return wrapper


def _to_item(row: asyncpg.Record) -> Item:
    return Item(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        project_id=row["project_id"],
    )


class PostgresItemRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @_translating
    async def get(self, item_id: str) -> Optional[Item]:
        row = await self._pool.fetchrow(
            "SELECT id, name, description, project_id FROM items WHERE id = $1", item_id
        )
        return _to_item(row) if row else None

    @_translating
    async def get_by_name(self, name: str) -> Optional[Item]:
        row = await self._pool.fetchrow(
            "SELECT id, name, description, project_id FROM items "
            "WHERE lower(btrim(name)) = lower(btrim($1))",
            name,
        )
        return _to_item(row) if row else None

    @_translating
    async def list(self, limit: int = 100, offset: int = 0) -> Tuple[List[Item], int]:
        # One statement, so the page and the total come from the same snapshot with no
        # transaction (BEGIN/COMMIT would be two more round trips).
        rows = await self._pool.fetch(
            "SELECT id, name, description, project_id, count(*) OVER () AS total FROM items "
            "ORDER BY seq LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
        if rows:
            return [_to_item(r) for r in rows], rows[0]["total"]
        if offset == 0:
            return [], 0  # nothing at all
        # A page past the end carries no rows to read the total from: ask for it.
        return [], await self._pool.fetchval("SELECT count(*) FROM items")

    @_translating
    async def create(self, item: Item) -> Item:
        try:
            await self._pool.execute(
                "INSERT INTO items (id, name, description, project_id) VALUES ($1, $2, $3, $4)",
                item.id, item.name, item.description, item.project_id,
            )
        except asyncpg.UniqueViolationError as exc:
            if "items_name_unique" in (exc.constraint_name or ""):
                raise DuplicateItemNameError(item.name) from exc
            raise
        return item

    @_translating
    async def update(self, item: Item) -> Optional[Item]:
        try:
            row = await self._pool.fetchrow(
                "UPDATE items SET name = $2, description = $3, project_id = $4 "
                "WHERE id = $1 RETURNING id, name, description, project_id",
                item.id, item.name, item.description, item.project_id,
            )
        except asyncpg.UniqueViolationError as exc:
            if "items_name_unique" in (exc.constraint_name or ""):
                raise DuplicateItemNameError(item.name) from exc
            raise
        return _to_item(row) if row else None

    @_translating
    async def delete(self, item_id: str) -> bool:
        result = await self._pool.execute("DELETE FROM items WHERE id = $1", item_id)
        return result.endswith(" 1")

    @_translating
    async def ping(self) -> None:
        await self._pool.fetchval("SELECT 1")
