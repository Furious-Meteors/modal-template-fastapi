"""sql/003_publish_event.sql: the one-round-trip publish, its ordering lock, and its lockdown."""
import re
from pathlib import Path

import pytest
import pytest_asyncio

from tests.conftest import LISTEN_URL, TEST_DATABASE_URL, fresh_pg_pool, requires_listen, requires_postgres

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "src" / "adapters" / "persistence" / "sql"
SQL = (SQL_DIR / "003_publish_event.sql").read_text()
LOCKDOWN = (SQL_DIR / "R__publish_event_lockdown.sql").read_text()
SIGNATURE = "publish_event(text, text, jsonb)"


def test_the_sql_lock_key_matches_the_python_constant():
    from src.adapters.events.postgres import _PUBLISH_LOCK_KEY

    keys = re.findall(r"pg_advisory_xact_lock\((\d+)\)", SQL)
    assert keys == [str(_PUBLISH_LOCK_KEY)], "the function and PostgresEventBus must agree on the ordering lock"


def test_the_function_is_locked_down_for_the_rest_api_roles():
    assert "REVOKE ALL ON FUNCTION publish_event(text, text, jsonb) FROM PUBLIC" in LOCKDOWN
    for role in ("anon", "authenticated", "service_role"):
        assert f"'{role}'" in LOCKDOWN
    assert "SET search_path" in SQL  # no search_path hijacking
    # the lockdown is repeatable (re-asserted every start), not a one-off in the versioned file
    assert "REVOKE" not in SQL


@pytest_asyncio.fixture
async def pool():
    p = await fresh_pg_pool()
    yield p
    await p.close()


@requires_postgres
@pytest.mark.asyncio
async def test_publish_event_returns_the_stored_event(pool):
    row = await pool.fetchrow("SELECT * FROM publish_event($1, $2, $3::jsonb)", "items", "item.created", '{"a": [1, null, "é"]}')
    assert (row["out_topic"], row["out_type"]) == ("items", "item.created")
    assert row["out_ts"] > 1_700_000_000
    stored = await pool.fetchrow("SELECT topic, type, data::text AS d FROM events WHERE seq = $1", row["out_seq"])
    assert (stored["topic"], stored["type"]) == ("items", "item.created")
    assert row["out_data"] == stored["d"]
    second = await pool.fetchrow("SELECT out_seq FROM publish_event('items', 'x', '{}')")
    assert second["out_seq"] > row["out_seq"]


@requires_postgres
@requires_listen
@pytest.mark.asyncio
async def test_publish_event_notifies_listeners_with_the_sequence_number(pool):
    import asyncio

    import asyncpg

    conn = await asyncpg.connect(LISTEN_URL, statement_cache_size=0)
    got: asyncio.Queue = asyncio.Queue()
    await conn.add_listener("events", lambda c, pid, ch, payload: got.put_nowait(payload))
    try:
        row = await pool.fetchrow("SELECT out_seq FROM publish_event('t', 'e', '{}')")
        assert await asyncio.wait_for(got.get(), 5) == str(row["out_seq"])
    finally:
        await conn.close()


@requires_postgres
@pytest.mark.asyncio
async def test_a_failed_publish_leaves_no_event_and_releases_the_lock(pool):
    import asyncpg

    with pytest.raises(asyncpg.PostgresError):
        await pool.fetchrow("SELECT * FROM publish_event($1, $2, $3::jsonb)", "t", None, "{}")  # type is NOT NULL
    assert await pool.fetchval("SELECT count(*) FROM events") == 0
    ok = await pool.fetchrow("SELECT out_seq FROM publish_event('t', 'e', '{}')")  # the lock is not stuck
    assert ok["out_seq"] > 0


@requires_postgres
@pytest.mark.asyncio
async def test_rest_api_roles_cannot_publish_and_the_lockdown_is_reasserted_on_every_start(pool):
    """Supabase grants EXECUTE on new public functions to its API roles; startup must take it back."""
    import asyncpg

    from src.adapters.persistence.postgres import ensure_schema
    from tests.conftest import is_local

    if not is_local(TEST_DATABASE_URL):
        pytest.skip("creates roles, which are cluster-wide: local Postgres only")
    created = []
    for role in ("anon", "authenticated", "service_role"):
        if not await pool.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
            await pool.execute(f'CREATE ROLE "{role}" NOLOGIN')
            created.append(role)
    try:
        for role in ("anon", "authenticated", "service_role"):  # simulate Supabase's default grants
            await pool.execute(f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO "{role}"')
            assert await pool.fetchval(f"SELECT has_function_privilege('{role}', '{SIGNATURE}', 'EXECUTE')") is True
        await ensure_schema(pool)  # what every container does at startup
        for role in ("anon", "authenticated", "service_role"):
            assert await pool.fetchval(f"SELECT has_function_privilege('{role}', '{SIGNATURE}', 'EXECUTE')") is False, role
        public_can = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "WHERE p.proname = 'publish_event' AND a.grantee = 0)")
        assert public_can is False
        assert await pool.fetchval(f"SELECT has_function_privilege(current_user, '{SIGNATURE}', 'EXECUTE')") is True  # the app still can
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('SET LOCAL ROLE "anon"')
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.fetchrow("SELECT * FROM publish_event('t', 'e', '{}')")
    finally:
        for role in created:
            await pool.execute(f'REVOKE ALL ON FUNCTION {SIGNATURE} FROM "{role}"')
            await pool.execute(f'DROP ROLE "{role}"')


@requires_postgres
@pytest.mark.asyncio
async def test_running_the_schema_twice_is_harmless(pool):
    from src.adapters.persistence.postgres import ensure_schema

    await ensure_schema(pool)
    await ensure_schema(pool)
    assert (await pool.fetchrow("SELECT out_seq FROM publish_event('t', 'e', '{}')"))["out_seq"] > 0
