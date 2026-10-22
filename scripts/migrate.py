#!/usr/bin/env python
"""
Inspect and apply schema migrations without starting the app.

    python scripts/migrate.py status            # what is applied / pending / changed (read-only)
    python scripts/migrate.py status --check    # same, exit 1 if anything is pending or changed (CI gate)
    python scripts/migrate.py up                # apply pending migrations (atomic, under the advisory lock)

Reads DATABASE_URL from the environment or .env (git-ignored). The password is never printed.

Typical use: run `up` as a deploy step and set AUTO_MIGRATE=false on the app, so containers
only VERIFY the schema at boot (they refuse to start on a database that is behind). With the
default AUTO_MIGRATE=true the first container to start migrates and the rest wait for it.

Migrations live in src/adapters/persistence/sql/: NNN_name.sql (versioned, applied once,
immutable afterwards) and R__name.sql (repeatable, re-run every time). See docs/persistence.md.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.dburl import describe_url, url_problems  # noqa: E402
from src.utils.envfile import load_env_file  # noqa: E402

MARK = {"applied": "applied   ", "pending": "PENDING   ", "changed": "CHANGED   ", "unknown": "unknown   ", "repeatable": "repeatable"}


async def run(args) -> int:
    from src.adapters.persistence.migrations import MigrationError, run_migrations, status
    from src.adapters.persistence.postgres import create_pool

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set (environment or .env).", file=sys.stderr)
        return 2
    errors, _ = url_problems(url, role="pool")
    info = describe_url(url)
    print(f"database: {info.masked}")
    if errors:
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return 2
    try:
        pool = await asyncio.wait_for(create_pool(url), timeout=30)
    except Exception as exc:  # class name only: never the DSN
        print(f"Could not connect: {type(exc).__name__}", file=sys.stderr)
        return 3
    try:
        if args.command == "up":
            report = await run_migrations(pool)
            for w in report.warnings:
                print(f"warning: {w}")
            if report.applied:
                print("applied: " + ", ".join(report.applied))
            else:
                print(f"nothing to apply ({report.already_applied} already applied)")
            if report.repeatable:
                print("re-ran repeatable: " + ", ".join(report.repeatable))
            return 0
        rows = await status(pool)
        for r in rows:
            version = f"{r['version']:03d}" if r["version"] is not None else " R "
            when = f"  {r['applied_at']:%Y-%m-%d %H:%M}" if r["applied_at"] else ""
            note = "  <- file edited after it was applied: restore it and add a new migration" if r["state"] == "changed" else (
                "  <- the database is newer than this code" if r["state"] == "unknown" else "")
            print(f"  {version}  {MARK[r['state']]}  {r['name']}{when}{note}")
        bad = [r for r in rows if r["state"] in ("pending", "changed")]
        print(f"\n{sum(r['state'] == 'applied' for r in rows)} applied, "
              f"{sum(r['state'] == 'pending' for r in rows)} pending, {sum(r['state'] == 'changed' for r in rows)} changed")
        return 1 if (bad and args.check) else 0
    except MigrationError as exc:
        print(f"MIGRATION FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await pool.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["status", "up"])
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    p.add_argument("--check", action="store_true", help="with status: exit 1 if anything is pending or changed")
    args = p.parse_args(argv)
    load_env_file(args.env_file)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
