"""
src/adapters/events/postgres.py
===============================
Shared streaming state in Postgres, so streams work across Modal containers:

  PostgresEventBus       EventBus   — events table + NOTIFY, one pump per container
  PostgresWebhookStore   WebhookEventStore — atomic via a primary key
  PostgresTicketStore    TicketReplayStore — atomic via a primary key

How the bus works
-----------------
publish() inserts a row and NOTIFYs inside one transaction. Each container runs
a single background pump that reads rows with seq > cursor and fans them out to
that container's local subscribers (in-process queues, like the in-memory bus).

* Ordering: publishers take a transaction-level advisory lock before inserting,
  so seq order equals commit order. A reader's "seq > cursor" therefore never
  skips a row that commits late. Publishing is serialised by this lock: measured
  at roughly 400-600 events/s on a laptop with local Postgres — ample for
  notifications and activity feeds, not for high-volume streams.
* Latency: with EVENT_BUS_LISTEN_URL set, a dedicated LISTEN connection wakes the
  pump immediately. Without it the pump polls every EVENT_BUS_POLL_MS.
  LISTEN needs a *session*-level connection: Supabase's transaction pooler
  (port 6543) cannot do it — use the direct connection or the session pooler
  (port 5432) for EVENT_BUS_LISTEN_URL. Everything else (publish, polling,
  replay) works fine through the transaction pooler.
* Replay: served from the events table (the latest EVENT_REPLAY_SIZE rows after
  Last-Event-ID). Rows are pruned by age (EVENT_RETENTION_SECONDS) and by count.
* Slow consumers are disconnected rather than buffered, as in the memory bus.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, Optional, Set

import asyncpg

from src.adapters.persistence.postgres import _ssl_setting, _translating
from src.core.domain.events import Event

logger = logging.getLogger(__name__)

_PUBLISH_LOCK_KEY = 7_340_002  # serialises publishers so seq order == commit order
_CHANNEL = "events"
_PROBE_CHANNEL = "events_probe"
_PROBE_RETRY_SECONDS = 30
_BATCH = 500
_PRUNE_EVERY_SECONDS = 60
_MAX_ROWS = 100_000
_CLOSE = object()

_COLUMNS = "seq, topic, type, data::text AS data, extract(epoch FROM ts)::float8 AS ts"


def _to_event(row: asyncpg.Record) -> Event:
    return Event(
        id=str(row["seq"]),
        type=row["type"],
        data=json.loads(row["data"]),
        ts=row["ts"],
    )


class _Subscriber:
    __slots__ = ("queue", "ready")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.ready = False  # True once its starting point in the log is fixed


class PostgresEventBus:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        listen_dsn: Optional[str] = None,
        listen_probe_timeout: float = 3.0,
        poll_interval: float = 1.0,
        replay_size: int = 100,
        subscriber_queue_size: int = 256,
        retention_seconds: int = 3600,
        max_rows: int = _MAX_ROWS,
    ) -> None:
        self._pool = pool
        self._listen_dsn = listen_dsn
        self._probe_timeout = listen_probe_timeout
        self._poll_interval = poll_interval
        self._replay_size = replay_size
        self._queue_size = subscriber_queue_size
        self._retention = retention_seconds
        self._max_rows = max_rows
        self._subscribers: Dict[str, Set[_Subscriber]] = {}
        self._cursor = 0
        self._wake = asyncio.Event()
        self._listening = False
        self._closing = False
        self._tasks: list[asyncio.Task] = []

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Begin delivering events published from now on. Idempotent."""
        if self._tasks:
            return
        self._cursor = await self._pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
        self._closing = False
        self._tasks.append(asyncio.create_task(self._pump(), name="event-bus-pump"))
        if self._listen_dsn:
            self._tasks.append(asyncio.create_task(self._listen(), name="event-bus-listen"))
        logger.info(
            "PostgresEventBus started (cursor=%s, %s)",
            self._cursor,
            "LISTEN/NOTIFY" if self._listen_dsn else f"polling every {self._poll_interval}s",
        )

    async def close(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for subs in self._subscribers.values():
            for sub in list(subs):
                self._close_subscriber(sub)

    # ── publish / subscribe ────────────────────────────────────────────────
    @_translating
    async def publish(self, topic: str, event_type: str, data: Dict[str, Any]) -> Event:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", _PUBLISH_LOCK_KEY)
                row = await conn.fetchrow(
                    f"INSERT INTO events (topic, type, data) VALUES ($1, $2, $3::jsonb) "
                    f"RETURNING {_COLUMNS}",
                    topic, event_type, json.dumps(data),
                )
                await conn.execute("SELECT pg_notify($1, $2)", _CHANNEL, str(row["seq"]))
        return _to_event(row)

    async def subscribe(
        self, topic: str, last_event_id: Optional[str] = None
    ) -> AsyncIterator[Event]:
        sub = _Subscriber(self._queue_size)
        # Register first so nothing the pump dispatches from now on is missed,
        # then fix the starting point from the DATABASE, not from the pump's
        # cursor (which can lag a little): reg_seq is the newest event that
        # already exists. Events <= reg_seq are served by replay (or skipped
        # when no replay was asked for); events > reg_seq come through the
        # queue; queued items <= reg_seq are dropped, so nothing is delivered
        # twice and nothing published before subscribing leaks in.
        self._subscribers.setdefault(topic, set()).add(sub)
        try:
            try:
                reg_seq = await self._pool.fetchval("SELECT coalesce(max(seq), 0) FROM events")
            except Exception:
                logger.warning("Could not read the log head; using the pump cursor", exc_info=True)
                reg_seq = self._cursor
            sub.ready = True
            for event in await self._replay(topic, last_event_id, reg_seq):
                yield event
            while True:
                item = await sub.queue.get()
                if item is _CLOSE:
                    return
                if int(item.id) <= reg_seq:
                    continue
                yield item
        finally:
            self._subscribers.get(topic, set()).discard(sub)

    def subscriber_count(self, topic: str) -> int:
        """Subscribers that are fully set up (their starting point is fixed)."""
        return sum(1 for s in self._subscribers.get(topic, ()) if s.ready)

    async def _replay(self, topic: str, last_event_id: Optional[str], up_to: int) -> list[Event]:
        if last_event_id is None:
            return []
        try:
            last = int(last_event_id)
        except ValueError:
            return []
        try:
            rows = await self._pool.fetch(
                f"SELECT * FROM (SELECT {_COLUMNS} FROM events "
                f"WHERE topic = $1 AND seq > $2 AND seq <= $3 ORDER BY seq DESC LIMIT $4) r "
                f"ORDER BY seq",
                topic, last, up_to, self._replay_size,
            )
        except Exception:
            # Replay is best-effort by contract: carry on with live events.
            logger.warning("Event replay failed; continuing live-only", exc_info=True)
            return []
        return [_to_event(r) for r in rows]

    # ── background work ────────────────────────────────────────────────────
    def _dispatch(self, topic: str, event: Event) -> None:
        for sub in list(self._subscribers.get(topic, ())):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers[topic].discard(sub)
                self._close_subscriber(sub)

    @staticmethod
    def _close_subscriber(sub: _Subscriber) -> None:
        while not sub.queue.empty():
            sub.queue.get_nowait()
        sub.queue.put_nowait(_CLOSE)

    async def _drain(self) -> None:
        # Dispatch in chunks of half a subscriber queue and yield between them.
        # Handing a whole batch (up to _BATCH rows) to the queues in one go
        # would overflow them before even a perfectly healthy consumer got
        # scheduled, and the slow-consumer policy would disconnect it. Yielding
        # lets consumers drain; only genuinely slow ones still fall behind.
        chunk = max(1, self._queue_size // 2)
        while True:
            rows = await self._pool.fetch(
                f"SELECT {_COLUMNS} FROM events WHERE seq > $1 ORDER BY seq LIMIT $2",
                self._cursor, _BATCH,
            )
            for i, row in enumerate(rows):
                self._dispatch(row["topic"], _to_event(row))
                self._cursor = row["seq"]
                if (i + 1) % chunk == 0:
                    await asyncio.sleep(0)
            if len(rows) < _BATCH:
                return

    async def _pump(self) -> None:
        last_prune = time.monotonic()
        backoff = 0.5
        while not self._closing:
            try:
                await self._drain()
                backoff = 0.5
                if time.monotonic() - last_prune >= _PRUNE_EVERY_SECONDS:
                    last_prune = time.monotonic()
                    await self._prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Event pump error; retrying in %.1fs", backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            # With LISTEN a NOTIFY wakes us at once; the timeout is only a safety net.
            timeout = max(self._poll_interval, 5.0) if self._listening else self._poll_interval
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)
            self._wake.clear()

    async def _prune(self) -> None:
        await self._pool.execute(
            "DELETE FROM events WHERE ts < now() - make_interval(secs => $1)", float(self._retention)
        )
        await self._pool.execute(
            "DELETE FROM events WHERE seq <= (SELECT max(seq) FROM events) - $1", self._max_rows
        )

    async def _listen(self) -> None:
        """Hold a dedicated session connection LISTENing; reconnect on loss."""
        backoff = 1.0
        while not self._closing:
            conn = None
            lost = asyncio.Event()
            probe = asyncio.Event()
            try:
                kwargs: dict = {"statement_cache_size": 0}
                ssl = _ssl_setting(self._listen_dsn)
                if ssl:
                    kwargs["ssl"] = ssl
                conn = await asyncpg.connect(self._listen_dsn, **kwargs)
                await conn.add_listener(_CHANNEL, lambda *_: self._wake.set())
                await conn.add_listener(_PROBE_CHANNEL, lambda *_: probe.set())
                conn.add_termination_listener(lambda *_: lost.set())
                # A connection can succeed and still never hear a NOTIFY — notably a
                # transaction-mode pooler (Supabase port 6543), or a different database.
                # Believing it would leave us on the slow safety poll without a word, so
                # prove it: publish a probe from ANOTHER connection (the pool) and wait.
                await self._pool.execute("SELECT pg_notify($1, 'probe')", _PROBE_CHANNEL)
                try:
                    await asyncio.wait_for(probe.wait(), self._probe_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "EVENT_BUS_LISTEN_URL connected but did not receive a test NOTIFY — it looks "
                        "like a transaction-mode pooler (Supabase port 6543) or the wrong database. "
                        "LISTEN is disabled; events are picked up by polling every %.1fs. Use the "
                        "session pooler or direct connection (port 5432) for instant delivery.",
                        self._poll_interval,
                    )
                    await asyncio.sleep(_PROBE_RETRY_SECONDS)
                    continue
                self._listening = True
                self._wake.set()  # catch anything published while we were not listening
                backoff = 1.0
                await lost.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Event LISTEN connection failed; polling continues", exc_info=True)
            finally:
                self._listening = False
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close(timeout=2)
            if not self._closing:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)


class _Pruner:
    """Run a cheap DELETE at most once per interval, piggybacking on writes."""

    def __init__(self, every: float = 60.0) -> None:
        self._every = every
        self._last = 0.0

    def due(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self._every:
            self._last = now
            return True
        return False


class PostgresWebhookStore:
    def __init__(self, pool: asyncpg.Pool, ttl_seconds: int = 86_400) -> None:
        self._pool = pool
        self._ttl = float(ttl_seconds)
        self._pruner = _Pruner()

    @_translating
    async def record_if_new(self, provider: str, webhook_id: str) -> bool:
        # Atomic: inserts a new key, or re-claims an expired one; a live duplicate
        # matches the conflict but fails the WHERE, so no row comes back.
        row = await self._pool.fetchrow(
            "INSERT INTO webhook_events (provider, webhook_id) VALUES ($1, $2) "
            "ON CONFLICT (provider, webhook_id) DO UPDATE SET created_at = now() "
            "WHERE webhook_events.created_at < now() - make_interval(secs => $3) "
            "RETURNING 1",
            provider, webhook_id, self._ttl,
        )
        if self._pruner.due():
            await self._pool.execute(
                "DELETE FROM webhook_events WHERE created_at < now() - make_interval(secs => $1)",
                self._ttl,
            )
        return row is not None

    @_translating
    async def forget(self, provider: str, webhook_id: str) -> None:
        await self._pool.execute(
            "DELETE FROM webhook_events WHERE provider = $1 AND webhook_id = $2",
            provider, webhook_id,
        )


class PostgresTicketStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._pruner = _Pruner()

    @_translating
    async def consume(self, jti: str, expires_at: float) -> bool:
        result = await self._pool.execute(
            "INSERT INTO ws_ticket_uses (jti, expires_at) VALUES ($1, to_timestamp($2)) "
            "ON CONFLICT (jti) DO NOTHING",
            jti, float(expires_at),
        )
        if self._pruner.due():
            await self._pool.execute("DELETE FROM ws_ticket_uses WHERE expires_at < now()")
        return result.endswith(" 1")
