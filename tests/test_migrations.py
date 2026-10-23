"""
Versioned schema migrations: ordering, adoption of existing databases, atomicity,
concurrency, drift detection, rollbacks of the app, and the AUTO_MIGRATE=false gate.

Discovery tests need no database; the rest run against TEST_DATABASE_URL (a disposable one).
"""
import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from src.adapters.persistence import migrations as mig
from src.adapters.persistence.migrations import MigrationError
from tests.conftest import TEST_DATABASE_URL, requires_postgres, wipe_database

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "src" / "adapters" / "persistence" / "sql"


def write(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql)
    return path


@pytest.fixture
def shipped_copy(tmp_path):
    """
    A writable copy of the first BASELINE shipped migrations (and the repeatable scripts), to extend
    with new ones. These tests are about the runner's mechanics and are written around that baseline,
    so shipping another migration never breaks them.
    """
    target = tmp_path / "sql"
    shutil.copytree(
        SHIPPED, target,
        ignore=lambda _dir, names: [n for n in names if n[:3].isdigit() and int(n[:3]) > BASELINE],
    )
    return target


# ── discovery (no database) ─────────────────────────────────────────────────

def test_versions_sort_numerically_not_alphabetically(tmp_path):
    for name in ("10_c.sql", "2_b.sql", "001_a.sql"):
        write(tmp_path, name, "SELECT 1;")
    versioned, _ = mig.discover(tmp_path)
    assert [m.version for m in versioned] == [1, 2, 10]


def test_two_files_with_the_same_version_are_refused(tmp_path):
    write(tmp_path, "001_a.sql", "SELECT 1;")
    write(tmp_path, "1_b.sql", "SELECT 2;")
    with pytest.raises(MigrationError, match=r"share version 1.*001_a\.sql.*1_b\.sql"):
        mig.discover(tmp_path)


@pytest.mark.parametrize("bad", ["readme.sql", "04-oops.sql", "R_single.sql", "001.sql", "x_001.sql"])
def test_a_misnamed_file_is_refused_rather_than_silently_skipped(tmp_path, bad):
    write(tmp_path, bad, "SELECT 1;")
    with pytest.raises(MigrationError, match="does not match"):
        mig.discover(tmp_path)


def test_non_sql_files_are_ignored_and_repeatables_are_separate(tmp_path):
    write(tmp_path, "001_a.sql", "SELECT 1;")
    write(tmp_path, "R__b.sql", "SELECT 2;")
    write(tmp_path, "R__a.sql", "SELECT 3;")
    (tmp_path / "README.md").write_text("notes")
    versioned, repeatable = mig.discover(tmp_path)
    assert [m.filename for m in versioned] == ["001_a.sql"]
    assert [m.filename for m in repeatable] == ["R__a.sql", "R__b.sql"] and all(m.repeatable for m in repeatable)


def test_the_checksum_ignores_line_endings_but_not_content():
    assert mig.checksum("a\r\nb\r\n") == mig.checksum("a\nb\n")
    assert mig.checksum("a") != mig.checksum("b")


def test_the_shipped_migrations_are_contiguous_and_well_formed():
    versioned, repeatable = mig.discover()
    assert [m.version for m in versioned] == list(range(1, len(versioned) + 1)), "version numbers must have no gaps"
    assert any(r.name == "publish_event_lockdown" for r in repeatable)
    for m in versioned:
        assert "REVOKE" not in m.sql or m.version == 0, f"{m.filename}: grants belong in a repeatable file"


def test_the_lock_key_is_the_one_the_old_scheme_used_so_rolling_deploys_serialise():
    assert mig.LOCK_KEY == 7_340_001


# ── against a database ──────────────────────────────────────────────────────

pg = requires_postgres


@pytest_asyncio.fixture
async def db():
    """A pristine database: nothing migrated, no app tables."""
    from src.adapters.persistence.postgres import create_pool

    pool = await create_pool(TEST_DATABASE_URL)
    await wipe_database(pool)
    await pool.execute("DROP TABLE IF EXISTS mig_demo, mig_log CASCADE")
    yield pool
    await wipe_database(pool)
    await pool.execute("DROP TABLE IF EXISTS mig_demo, mig_log CASCADE")
    await pool.close()


BASELINE = 3
NAMES = sorted(p.name for p in SHIPPED.glob("[0-9]*.sql"))  # every shipped versioned migration, in order


async def applied_versions(pool):
    return [r["version"] for r in await pool.fetch("SELECT version FROM schema_migrations ORDER BY version")]


@pg
@pytest.mark.asyncio
async def test_a_fresh_database_gets_everything_in_order_and_it_is_recorded(db):
    report = await mig.run_migrations(db)
    assert report.applied == NAMES
    assert report.repeatable == ["R__publish_event_lockdown.sql"]
    assert await applied_versions(db) == list(range(1, len(NAMES) + 1))
    for table in ("items", "events", "webhook_events", "ws_ticket_uses", "rate_limits", "schema_migrations"):
        assert await db.fetchval("SELECT to_regclass($1) IS NOT NULL", table), table
    assert (await db.fetchrow("SELECT out_seq FROM publish_event('t', 'e', '{}')"))["out_seq"] > 0
    assert await db.fetchval("SELECT relrowsecurity FROM pg_class WHERE relname = 'schema_migrations'") is True
    row = await db.fetchrow("SELECT checksum, execution_ms FROM schema_migrations WHERE version = 1")
    assert row["checksum"] == mig.discover()[0][0].checksum and row["execution_ms"] >= 0


@pg
@pytest.mark.asyncio
async def test_running_again_changes_nothing(db):
    await mig.run_migrations(db)
    before = await db.fetch("SELECT version, applied_at FROM schema_migrations ORDER BY version")
    report = await mig.run_migrations(db)
    assert report.applied == [] and report.already_applied == len(NAMES) and report.warnings == []
    assert await db.fetch("SELECT version, applied_at FROM schema_migrations ORDER BY version") == before


@pg
@pytest.mark.asyncio
async def test_an_existing_database_from_the_old_scheme_is_adopted_without_touching_its_data(db):
    """No schema_migrations, tables already there, rows already in them (the live Supabase case)."""
    for name in ("001_create_items.sql", "002_streaming_state.sql", "003_publish_event.sql", "R__publish_event_lockdown.sql"):
        await db.execute((SHIPPED / name).read_text())  # exactly what the old runner did
    await db.execute("INSERT INTO items (id, name) VALUES ('keep-1', 'Keep Me'), ('keep-2', 'And Me')")
    await db.fetchrow("SELECT * FROM publish_event('items', 'item.created', '{\"id\": \"keep-1\"}')")
    assert not await db.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL")

    report = await mig.run_migrations(db)
    assert report.applied == NAMES  # the old scheme's three are adopted; anything newer genuinely runs
    assert sorted(r["name"] for r in await db.fetch("SELECT name FROM items")) == ["And Me", "Keep Me"]
    assert await db.fetchval("SELECT count(*) FROM events") == 1
    assert all(r["state"] in ("applied", "repeatable") for r in await mig.status(db))
    assert (await mig.run_migrations(db)).applied == []


@pg
@pytest.mark.asyncio
async def test_a_later_migration_evolves_an_existing_table_and_keeps_its_data(db, shipped_copy):
    """The thing the old scheme could not do: change a table that already exists."""
    from src.adapters.persistence.postgres import PostgresItemRepository
    from src.core.domain.item import Item

    await mig.run_migrations(db, shipped_copy)
    repo = PostgresItemRepository(db)
    await repo.create(Item(id="old-1", name="Old One"))
    await repo.create(Item(id="old-2", name="Old Two"))

    write(shipped_copy, "004_add_priority.sql", "ALTER TABLE items ADD COLUMN priority integer NOT NULL DEFAULT 0;")
    write(shipped_copy, "005_backfill_priority.sql", "UPDATE items SET priority = 5 WHERE name = 'Old Two';")
    report = await mig.run_migrations(db, shipped_copy)
    assert report.applied == ["004_add_priority.sql", "005_backfill_priority.sql"]
    rows = {r["name"]: r["priority"] for r in await db.fetch("SELECT name, priority FROM items")}
    assert rows == {"Old One": 0, "Old Two": 5}  # data kept, default applied, backfill ran
    assert (await repo.get("old-1")).name == "Old One"  # the app still works against the evolved table
    await repo.create(Item(id="new-1", name="New One"))
    assert (await mig.run_migrations(db, shipped_copy)).applied == []  # and it is not applied twice


@pg
@pytest.mark.asyncio
async def test_a_failing_migration_leaves_no_trace_and_is_retried_after_the_fix(db, shipped_copy):
    await mig.run_migrations(db, shipped_copy)
    write(shipped_copy, "004_good.sql", "CREATE TABLE mig_demo (id integer);")
    bad = write(shipped_copy, "005_bad.sql", "THIS IS NOT SQL;")
    with pytest.raises(MigrationError, match=r"005_bad\.sql.*rolled back"):
        await mig.run_migrations(db, shipped_copy)
    assert not await db.fetchval("SELECT to_regclass('mig_demo') IS NOT NULL"), "004's table must not survive a failed batch"
    assert await applied_versions(db) == [1, 2, 3]  # nothing from the failed run is recorded

    bad.write_text("CREATE TABLE mig_log (id integer);")  # fix it: it was never applied, so editing is fine
    report = await mig.run_migrations(db, shipped_copy)
    assert report.applied == ["004_good.sql", "005_bad.sql"]
    assert await applied_versions(db) == [1, 2, 3, 4, 5]


@pg
@pytest.mark.asyncio
async def test_editing_an_applied_migration_is_refused_and_nothing_else_runs(db, shipped_copy):
    await mig.run_migrations(db, shipped_copy)
    (shipped_copy / "003_publish_event.sql").write_text((shipped_copy / "003_publish_event.sql").read_text() + "\n-- tweaked\n")
    write(shipped_copy, "004_next.sql", "CREATE TABLE mig_demo (id integer);")
    with pytest.raises(MigrationError, match=r"003_publish_event\.sql was changed after it was applied.*immutable.*new, higher-numbered"):
        await mig.run_migrations(db, shipped_copy)
    assert not await db.fetchval("SELECT to_regclass('mig_demo') IS NOT NULL")  # drift blocks the whole batch
    states = {r["file"]: r["state"] for r in await mig.status(db, shipped_copy)}
    assert states["003_publish_event.sql"] == "changed" and states["004_next.sql"] == "pending"


@pg
@pytest.mark.asyncio
async def test_many_containers_starting_together_apply_each_migration_exactly_once(db, shipped_copy):
    write(shipped_copy, "004_slow.sql", "SELECT pg_sleep(0.4); CREATE TABLE mig_demo (id integer);")
    reports = await asyncio.gather(*(mig.run_migrations(db, shipped_copy) for _ in range(10)))
    assert sorted(f for r in reports for f in r.applied) == [
        "001_create_items.sql", "002_streaming_state.sql", "003_publish_event.sql", "004_slow.sql"
    ], "each migration must be applied by exactly one of the ten runners"
    assert await applied_versions(db) == [1, 2, 3, 4]
    assert all(r.repeatable == ["R__publish_event_lockdown.sql"] for r in reports)  # repeatables still run for everyone


@pg
@pytest.mark.asyncio
async def test_a_database_newer_than_the_code_is_tolerated_so_rollbacks_are_not_blocked(db, caplog):
    await mig.run_migrations(db)
    await db.execute("INSERT INTO schema_migrations (version, name, checksum, execution_ms) VALUES (99, 'from_the_future', 'x', 1)")
    with caplog.at_level(logging.WARNING, logger="src.adapters.persistence.migrations"):
        report = await mig.run_migrations(db)
    assert report.applied == [] and any("does not know (99)" in w for w in report.warnings)
    assert any("does not know (99)" in r.getMessage() for r in caplog.records)
    assert await applied_versions(db) == [*range(1, len(NAMES) + 1), 99]  # never deleted
    assert {r["version"]: r["state"] for r in await mig.status(db)}[99] == "unknown"


@pg
@pytest.mark.asyncio
async def test_a_migration_numbered_below_an_applied_one_still_applies_with_a_warning(db, shipped_copy):
    write(shipped_copy, "005_five.sql", "CREATE TABLE mig_log (id integer);")
    await mig.run_migrations(db, shipped_copy)
    write(shipped_copy, "004_four.sql", "CREATE TABLE mig_demo (id integer);")  # e.g. merged from another branch
    report = await mig.run_migrations(db, shipped_copy)
    assert report.applied == ["004_four.sql"] and any("out of order" in w for w in report.warnings)
    assert await applied_versions(db) == [1, 2, 3, 4, 5]


@pg
@pytest.mark.asyncio
async def test_repeatable_migrations_run_every_time_after_the_versioned_ones(db, shipped_copy):
    write(shipped_copy, "R__touch.sql", "CREATE TABLE IF NOT EXISTS mig_log (n integer); INSERT INTO mig_log VALUES (1);")
    for _ in range(3):
        await mig.run_migrations(db, shipped_copy)
    assert await db.fetchval("SELECT count(*) FROM mig_log") == 3


@pg
@pytest.mark.asyncio
async def test_a_failing_repeatable_rolls_back_the_new_versioned_migrations_too(db, shipped_copy):
    await mig.run_migrations(db, shipped_copy)
    write(shipped_copy, "004_new.sql", "CREATE TABLE mig_demo (id integer);")
    write(shipped_copy, "R__broken.sql", "SELECT * FROM table_that_does_not_exist;")
    with pytest.raises(MigrationError, match=r"R__broken\.sql.*rolled back"):
        await mig.run_migrations(db, shipped_copy)
    assert not await db.fetchval("SELECT to_regclass('mig_demo') IS NOT NULL")
    assert await applied_versions(db) == [1, 2, 3]


@pg
@pytest.mark.asyncio
async def test_status_is_read_only_and_works_before_anything_exists(db):
    rows = await mig.status(db)
    assert {r["state"] for r in rows if r["file"] and not r["file"].startswith("R__")} == {"pending"}
    assert not await db.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL"), "status must not create anything"
    assert not await db.fetchval("SELECT to_regclass('items') IS NOT NULL")


@pg
@pytest.mark.asyncio
async def test_waiting_too_long_for_another_instance_is_a_clear_error_not_a_hang(db):
    async with db.acquire() as holder:
        async with holder.transaction():
            await holder.execute("SELECT pg_advisory_xact_lock($1)", mig.LOCK_KEY)  # "another container is migrating"
            started = asyncio.get_running_loop().time()
            with pytest.raises(MigrationError, match=r"Another instance has been running migrations for more than 1s"):
                await mig.run_migrations(db, lock_timeout_seconds=1)
            assert asyncio.get_running_loop().time() - started < 6
    await mig.run_migrations(db)  # and once the lock is free it works


@pg
@pytest.mark.asyncio
async def test_a_migration_that_runs_too_long_is_cut_off_and_rolled_back(db, shipped_copy):
    write(shipped_copy, "004_slow.sql", "CREATE TABLE mig_demo (id integer); SELECT pg_sleep(5);")
    with pytest.raises(MigrationError, match=r"004_slow\.sql failed \(TimeoutError"):
        await mig.run_migrations(db, shipped_copy, statement_timeout_seconds=1)
    assert not await db.fetchval("SELECT to_regclass('mig_demo') IS NOT NULL")


# ── AUTO_MIGRATE=false: verify, never migrate ───────────────────────────────

@pg
@pytest.mark.asyncio
async def test_with_auto_migrate_off_startup_refuses_a_database_that_is_behind(db, monkeypatch):
    monkeypatch.setenv("AUTO_MIGRATE", "false")
    with pytest.raises(MigrationError, match=r"AUTO_MIGRATE is off.*001_create_items\.sql \(pending\).*scripts/migrate\.py up"):
        await mig.migrate_on_startup(db)
    assert not await db.fetchval("SELECT to_regclass('items') IS NOT NULL"), "verify mode must not migrate"


@pg
@pytest.mark.asyncio
async def test_with_auto_migrate_off_an_up_to_date_database_starts_and_is_not_written_to(db, monkeypatch):
    await mig.run_migrations(db)
    before = await db.fetch("SELECT version, applied_at FROM schema_migrations ORDER BY version")
    monkeypatch.setenv("AUTO_MIGRATE", "false")
    assert await mig.migrate_on_startup(db) is None
    assert await db.fetch("SELECT version, applied_at FROM schema_migrations ORDER BY version") == before


@pg
@pytest.mark.asyncio
async def test_with_auto_migrate_off_a_changed_file_is_refused_too(db, shipped_copy, monkeypatch):
    await mig.run_migrations(db, shipped_copy)
    (shipped_copy / "001_create_items.sql").write_text("-- edited\n" + (shipped_copy / "001_create_items.sql").read_text())
    monkeypatch.setenv("AUTO_MIGRATE", "false")
    with pytest.raises(MigrationError, match=r"001_create_items\.sql \(changed\)"):
        await mig.migrate_on_startup(db, shipped_copy)


@pytest.mark.parametrize("value,expected", [("false", False), ("0", False), ("NO", False), ("off", False), ("true", True), ("", True), ("anything", True)])
def test_auto_migrate_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("AUTO_MIGRATE", value)
    assert mig.auto_migrate_enabled() is expected


@pg
@pytest.mark.asyncio
async def test_the_app_refuses_to_start_when_verification_fails(db, monkeypatch):
    """The lifespan path: AUTO_MIGRATE=false against an unmigrated database stops the container at boot."""
    from fastapi.testclient import TestClient

    from src import deps
    from src.main import app

    monkeypatch.setenv("PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("EVENT_BUS_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("AUTO_MIGRATE", "false")
    for cached in (deps._get_repository, deps._get_event_bus, deps._get_webhook_store, deps._get_ticket_store):
        cached.cache_clear()
    for dep in (deps.get_item_service, deps.get_event_bus, deps.get_webhook_store, deps.get_ticket_store):
        app.dependency_overrides.pop(dep, None)
    try:
        with pytest.raises(MigrationError, match="AUTO_MIGRATE is off"):
            with TestClient(app):
                pass
        assert deps._pool is None  # no leaked pool after the failed start
    finally:
        for cached in (deps._get_repository, deps._get_event_bus, deps._get_webhook_store, deps._get_ticket_store):
            cached.cache_clear()


# ── the CLI ─────────────────────────────────────────────────────────────────

def _cli(*args, env_extra=None):
    import os

    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env.update({"DATABASE_URL": TEST_DATABASE_URL or "", **(env_extra or {})})
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "migrate.py"), *args, "--env-file", "/nonexistent"],
                          cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)


@pg
def test_cli_status_check_gates_ci_and_up_applies(db):
    pending = _cli("status", "--check")
    assert pending.returncode == 1 and "PENDING" in pending.stdout and f"0 applied, {len(NAMES)} pending" in pending.stdout
    assert _cli("status").returncode == 0  # without --check it only reports
    up = _cli("up")
    assert up.returncode == 0 and "001_create_items.sql" in up.stdout and "re-ran repeatable" in up.stdout
    done = _cli("status", "--check")
    assert done.returncode == 0 and f"{len(NAMES)} applied, 0 pending, 0 changed" in done.stdout
    again = _cli("up")
    assert again.returncode == 0 and f"nothing to apply ({len(NAMES)} already applied)" in again.stdout


def test_cli_explains_missing_or_broken_configuration_without_leaking_the_password():
    secret = "S3cr3t-Pa55word"
    no_url = subprocess.run([sys.executable, str(ROOT / "scripts" / "migrate.py"), "status", "--env-file", "/nonexistent"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60,
                            env={k: v for k, v in __import__("os").environ.items() if k != "DATABASE_URL"})
    assert no_url.returncode == 2 and "DATABASE_URL is not set" in no_url.stderr
    placeholder = _cli("status", env_extra={"DATABASE_URL": f"postgresql://postgres.abcd:[{secret}]@h.pooler.supabase.com:6543/postgres"})
    assert placeholder.returncode == 2 and "wrapped in [ ]" in placeholder.stderr
    assert secret not in placeholder.stdout + placeholder.stderr
    unreachable = _cli("status", env_extra={"DATABASE_URL": f"postgresql://u:{secret}@127.0.0.1:1/db"})
    assert unreachable.returncode == 3 and "Could not connect" in unreachable.stderr
    assert secret not in unreachable.stdout + unreachable.stderr
