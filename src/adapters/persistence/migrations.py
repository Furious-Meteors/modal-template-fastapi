"""
src/adapters/persistence/migrations.py
======================================
Versioned schema migrations for the Postgres adapters.

Files live in sql/ next to this module:

    001_create_items.sql          VERSIONED   applied once, in numeric order, then recorded
    R__publish_event_lockdown.sql REPEATABLE  re-run on EVERY start, after the versioned ones

Rules (docs/persistence.md has the long version):

* A versioned file is IMMUTABLE once it has been applied anywhere. Its sha256 is stored in
  schema_migrations; changing it makes startup refuse to continue (drift), because other
  databases already ran the old text. To change the schema, add the next numbered file.
* Forward-only. There are no "down" scripts: undo a change with a new migration.
* The whole batch runs in ONE transaction under an advisory lock, so containers starting
  together serialise (the first migrates, the rest see nothing pending) and a failing
  migration leaves no trace (Postgres DDL is transactional) and is retried next start.
  Consequence: no CREATE INDEX CONCURRENTLY here — run those by hand.
* Rolling deploys mean old containers keep running against the migrated schema, so migrations
  must be backward compatible (add, backfill, and only drop what no running code reads).
* A database that is NEWER than this code (a rolled-back deploy) is tolerated with a warning,
  so rolling back is never blocked by a migration the old code does not know.
* Repeatable files must be idempotent. Use them for things that must hold on every start:
  grants, revokes, views, functions kept as code.

Adoption: databases created by the earlier "re-run every file" scheme have the tables but no
schema_migrations. 001-003 are idempotent, so the first run simply executes them again
(changing nothing, losing nothing) and records them.

AUTO_MIGRATE=false makes startup VERIFY instead of migrate (refuse to start if anything is
pending or changed), for teams that migrate as a deploy step (scripts/migrate.py up).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import asyncpg

from src.utils.config import MIGRATION_LOCK_TIMEOUT_SECONDS, MIGRATION_STATEMENT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parent / "sql"
# The key the previous "re-run every file" scheme used, so containers on old and new code
# still serialise with each other during a rolling deploy.
LOCK_KEY = 7_340_001

_VERSIONED = re.compile(r"^(\d+)_([A-Za-z0-9][A-Za-z0-9_\-]*)\.sql$")
_REPEATABLE = re.compile(r"^R__([A-Za-z0-9][A-Za-z0-9_\-]*)\.sql$")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      integer     PRIMARY KEY,
    name         text        NOT NULL,
    checksum     text        NOT NULL,
    applied_at   timestamptz NOT NULL DEFAULT now(),
    execution_ms integer     NOT NULL
);
-- Same reasoning as the app's other tables: keep it out of Supabase's REST API.
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
"""


class MigrationError(RuntimeError):
    """Something the operator can fix. The message is safe to print (no connection details)."""


@dataclass(frozen=True)
class Migration:
    version: Optional[int]  # None for repeatable
    name: str
    filename: str
    sql: str
    checksum: str

    @property
    def repeatable(self) -> bool:
        return self.version is None


@dataclass
class Report:
    applied: list[str] = field(default_factory=list)       # filenames applied this run
    repeatable: list[str] = field(default_factory=list)    # repeatable files re-run
    warnings: list[str] = field(default_factory=list)
    already_applied: int = 0


def checksum(sql: str) -> str:
    return hashlib.sha256(sql.replace("\r\n", "\n").encode()).hexdigest()


def discover(directory: Path = DEFAULT_DIR) -> tuple[list[Migration], list[Migration]]:
    """(versioned sorted by number, repeatable sorted by name). Raises MigrationError on a bad layout."""
    versioned: dict[int, Migration] = {}
    repeatable: list[Migration] = []
    for path in sorted(Path(directory).glob("*.sql")):
        sql = path.read_text()
        if m := _VERSIONED.match(path.name):
            version = int(m.group(1))
            if version in versioned:
                raise MigrationError(f"Two migrations share version {version}: {versioned[version].filename} and {path.name}")
            versioned[version] = Migration(version, m.group(2), path.name, sql, checksum(sql))
        elif m := _REPEATABLE.match(path.name):
            repeatable.append(Migration(None, m.group(1), path.name, sql, checksum(sql)))
        else:
            raise MigrationError(
                f"{path.name} does not match NNN_name.sql (versioned) or R__name.sql (repeatable); "
                "rename it or move it out of the migrations directory"
            )
    return [versioned[v] for v in sorted(versioned)], sorted(repeatable, key=lambda r: r.filename)


def _first_line(exc: BaseException) -> str:
    return (str(exc).splitlines() or [""])[0][:300]


def _drift(applied_checksum: str, m: Migration) -> bool:
    return applied_checksum != m.checksum


async def _applied_rows(conn) -> dict[int, asyncpg.Record]:
    if not await conn.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL"):
        return {}
    return {r["version"]: r for r in await conn.fetch("SELECT version, name, checksum, applied_at FROM schema_migrations")}


async def status(pool: asyncpg.Pool, directory: Path = DEFAULT_DIR) -> list[dict]:
    """Read-only: one row per migration with state applied | pending | changed | unknown (DB newer than code)."""
    versioned, repeatable = discover(directory)
    async with pool.acquire() as conn:
        applied = await _applied_rows(conn)
    rows = []
    for m in versioned:
        row = applied.get(m.version)
        state = "pending" if row is None else ("changed" if _drift(row["checksum"], m) else "applied")
        rows.append({"version": m.version, "name": m.name, "file": m.filename, "state": state,
                     "applied_at": row["applied_at"] if row else None})
    known = {m.version for m in versioned}
    for version in sorted(set(applied) - known):
        rows.append({"version": version, "name": applied[version]["name"], "file": None, "state": "unknown",
                     "applied_at": applied[version]["applied_at"]})
    for r in repeatable:
        rows.append({"version": None, "name": r.name, "file": r.filename, "state": "repeatable", "applied_at": None})
    return rows


async def run_migrations(
    pool: asyncpg.Pool,
    directory: Path = DEFAULT_DIR,
    *,
    lock_timeout_seconds: Optional[int] = None,
    statement_timeout_seconds: Optional[int] = None,
) -> Report:
    """Apply every pending versioned migration, then every repeatable one, atomically."""
    lock_s = int(lock_timeout_seconds or MIGRATION_LOCK_TIMEOUT_SECONDS)
    stmt_s = int(statement_timeout_seconds or MIGRATION_STATEMENT_TIMEOUT_SECONDS)
    versioned, repeatable = discover(directory)
    report = Report()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                # lock_timeout also bounds table-lock waits inside a migration, so a stuck
                # ALTER TABLE cannot freeze traffic indefinitely.
                await conn.execute(f"SET LOCAL lock_timeout = '{lock_s * 1000}ms'")
                try:
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
                except asyncpg.LockNotAvailableError:
                    raise MigrationError(
                        f"Another instance has been running migrations for more than {lock_s}s. "
                        "Check for a stuck migration (pg_stat_activity) or raise MIGRATION_LOCK_TIMEOUT_SECONDS."
                    ) from None
                await conn.execute(_CREATE_TABLE)
                applied = await _applied_rows(conn)
                by_version = {m.version: m for m in versioned}

                for version, row in sorted(applied.items()):
                    if version in by_version and _drift(row["checksum"], by_version[version]):
                        raise MigrationError(
                            f"Migration {by_version[version].filename} was changed after it was applied "
                            f"(applied {row['applied_at']:%Y-%m-%d}). Applied migrations are immutable: restore the "
                            "file and put the change in a new, higher-numbered migration."
                        )
                unknown = sorted(set(applied) - set(by_version))
                if unknown:
                    report.warnings.append(
                        f"database has migrations this code does not know ({', '.join(map(str, unknown))}): "
                        "running older code against a newer schema; continuing"
                    )
                pending = [m for m in versioned if m.version not in applied]
                newest = max(applied, default=0)
                for m in pending:
                    if m.version < newest:
                        report.warnings.append(f"applying {m.filename} out of order (version {newest} is already applied)")
                report.already_applied = len(applied)

                for m in pending:
                    started = time.perf_counter()
                    try:
                        await conn.execute(m.sql, timeout=stmt_s)
                    except MigrationError:
                        raise
                    except Exception as exc:
                        raise MigrationError(
                            f"Migration {m.filename} failed ({type(exc).__name__}: {_first_line(exc)}). "
                            "The whole batch was rolled back; nothing from this run was applied."
                        ) from exc
                    ms = int((time.perf_counter() - started) * 1000)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, execution_ms) VALUES ($1, $2, $3, $4)",
                        m.version, m.name, m.checksum, ms,
                    )
                    report.applied.append(m.filename)
                    logger.info("Applied migration %s (%d ms)", m.filename, ms)
                for r in repeatable:
                    try:
                        await conn.execute(r.sql, timeout=stmt_s)
                    except Exception as exc:
                        raise MigrationError(
                            f"Repeatable migration {r.filename} failed ({type(exc).__name__}: {_first_line(exc)}). "
                            "The whole batch was rolled back."
                        ) from exc
                    report.repeatable.append(r.filename)
        except MigrationError:
            raise
        except asyncpg.LockNotAvailableError:
            raise MigrationError(f"A migration waited more than {lock_s}s for a database lock and was rolled back.") from None
    for warning in report.warnings:
        logger.warning("migrations: %s", warning)
    return report


async def verify_up_to_date(pool: asyncpg.Pool, directory: Path = DEFAULT_DIR) -> None:
    """For AUTO_MIGRATE=false: refuse to start unless every migration is applied and unchanged."""
    problems = [r for r in await status(pool, directory) if r["state"] in ("pending", "changed")]
    if problems:
        listing = ", ".join(f"{r['file']} ({r['state']})" for r in problems)
        raise MigrationError(
            f"AUTO_MIGRATE is off and the database is not at this code's schema: {listing}. "
            "Run `python scripts/migrate.py up` (as a deploy step) before starting this version."
        )


def auto_migrate_enabled() -> bool:
    return os.environ.get("AUTO_MIGRATE", "true").strip().lower() not in ("false", "0", "no", "off")


async def migrate_on_startup(pool: asyncpg.Pool, directory: Path = DEFAULT_DIR) -> Optional[Report]:
    """What every container does at boot: migrate, or (AUTO_MIGRATE=false) verify."""
    if not auto_migrate_enabled():
        await verify_up_to_date(pool, directory)
        logger.info("AUTO_MIGRATE is off: schema verified up to date")
        return None
    report = await run_migrations(pool, directory)
    if report.applied:
        logger.info("Schema migrated: %d applied (%s)", len(report.applied), ", ".join(report.applied))
    return report
