#!/usr/bin/env python
"""
Check your Supabase (or any Postgres) connection settings before running the app.

    python scripts/check_supabase.py

Reads .env (git-ignored), then for DATABASE_URL, EVENT_BUS_LISTEN_URL and the
optional TEST_* URLs: validates the string, connects with the SAME pool settings
the app uses, and reports what it found. READ-ONLY — it creates and drops
nothing — and the password is never printed.

Exit code 0 when DATABASE_URL works; non-zero otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.dburl import database_identity, describe_url, is_local, url_problems  # noqa: E402
from src.utils.envfile import load_env_file  # noqa: E402

TABLES = ("items", "events", "webhook_events", "ws_ticket_uses")


def say(tag: str, message: str) -> None:
    print(f"  [{tag}] {message}")


async def check_pool(url: str) -> bool:
    from src.adapters.persistence.postgres import create_pool

    started = time.perf_counter()
    try:
        pool = await asyncio.wait_for(create_pool(url), timeout=20)
    except Exception as exc:  # report the class and first line only: never the DSN
        say("FAIL", f"could not connect: {type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}")
        say("hint", "check the password, the user (postgres.<project-ref> on a pooler), the region host, and your network (IPv6 for direct connections).")
        return False
    try:
        row = await pool.fetchrow("SELECT version() AS v, current_user AS u, current_database() AS d")
        ms = (time.perf_counter() - started) * 1000
        say("ok", f"connected as '{row['u']}' to '{row['d']}' — {row['v'].split(',')[0]} ({ms:.0f} ms incl. pool setup)")
        try:
            ssl_on = await pool.fetchval("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            say("ok" if ssl_on else "warn", f"connection encrypted: {'yes' if ssl_on else 'no / not reported through the pooler'}")
        except Exception:
            say("info", "could not read SSL status (normal behind a pooler)")
        existing = [
            r["tablename"] for r in await pool.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY($1::text[])", list(TABLES))
        ]
        if not existing:
            say("ok", "none of the app's tables exist yet — they are created on first start")
        for table in existing:
            n = await pool.fetchval(f'SELECT count(*) FROM "{table}"')
            say("info", f"table {table}: {n} row(s)")
        return True
    finally:
        await pool.close()


async def check_listen(url: str) -> bool:
    import asyncpg

    from src.adapters.persistence.postgres import _ssl_setting

    kwargs: dict = {"statement_cache_size": 0}
    if _ssl_setting(url):
        kwargs["ssl"] = _ssl_setting(url)
    try:
        conn = await asyncio.wait_for(asyncpg.connect(url, **kwargs), timeout=20)
    except Exception as exc:  # class name only: never the DSN
        say("FAIL", f"could not connect: {type(exc).__name__}")
        return False
    got = asyncio.Event()
    publisher = None
    try:
        await conn.add_listener("check_supabase", lambda *a: got.set())
        # Publish from a DIFFERENT connection, as the app does. Using the listener's own
        # connection would pass even through a transaction pooler and prove nothing.
        publisher = await asyncio.wait_for(asyncpg.connect(url, **kwargs), timeout=20)
        await publisher.execute("SELECT pg_notify('check_supabase', 'ping')")
        await asyncio.wait_for(got.wait(), timeout=5)
        say("ok", "LISTEN/NOTIFY from a second connection arrived — instant event delivery is available")
        return True
    except asyncio.TimeoutError:
        say("FAIL", "connected, but a NOTIFY from another connection never arrived: this looks like a "
                    "transaction-mode connection (Supabase port 6543) — use the session pooler / direct string (port 5432)")
        return False
    except Exception as exc:
        say("FAIL", f"LISTEN test failed: {type(exc).__name__}")
        return False
    finally:
        if publisher is not None:
            await publisher.close()
        await conn.close()


def validate(name: str, url: str, role: str) -> tuple[bool, str]:
    info = describe_url(url)
    print(f"\n{name}")
    print(f"  {info.masked}   [{info.kind}]")
    errors, warnings = url_problems(url, role=role)
    for message in warnings:
        say("warn", message)
    for message in errors:
        say("FAIL", message)
    return (not errors), url


async def run(env_file: str) -> int:
    if not Path(env_file).is_file():
        print(f"No {env_file}. Create it:  cp .env.example .env  (then fill it in)", file=sys.stderr)
        return 2
    load_env_file(env_file)
    failed = False

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("DATABASE_URL is not set in .env", file=sys.stderr)
        return 2
    ok, _ = validate("DATABASE_URL  (normal queries)", database_url, "pool")
    ok = ok and await check_pool(database_url)
    failed |= not ok

    listen = os.environ.get("EVENT_BUS_LISTEN_URL", "")
    if os.environ.get("EVENT_BUS_BACKEND") == "postgres":
        if not listen:
            print("\nEVENT_BUS_LISTEN_URL  (not set)")
            say("info", "events will be picked up by polling (up to EVENT_BUS_POLL_MS late). Set it for instant delivery.")
        else:
            ok, _ = validate("EVENT_BUS_LISTEN_URL  (instant event delivery)", listen, "listen")
            if ok:
                ok = await check_listen(listen)
            if not ok:
                say("info", "the app still works without it, by polling — this is not fatal.")

    test_url = os.environ.get("TEST_DATABASE_URL", "")
    if test_url:
        validate("TEST_DATABASE_URL  (pytest — DROPS the app's tables)", test_url, "pool")
        if not is_local(test_url) and os.environ.get("ALLOW_DESTRUCTIVE_DB_TESTS") != "yes":
            say("warn", "pytest will refuse to run until ALLOW_DESTRUCTIVE_DB_TESTS=yes is set (it drops tables).")
        if database_identity(database_url) == database_identity(test_url):
            say("FAIL", "TEST_DATABASE_URL points at the SAME database as DATABASE_URL: the tests would wipe your app data. Use a separate project.")
            failed = True

    print("\n" + ("Problems found — fix the [FAIL] lines above." if failed else "DATABASE_URL looks good. Next: python scripts/run_local.py"))
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    raise SystemExit(asyncio.run(run(parser.parse_args().env_file)))
