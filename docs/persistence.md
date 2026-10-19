# Persistence (Postgres / Supabase)

`PERSISTENCE_BACKEND` selects where items live:

| Value | Use for |
|---|---|
| `memory` (default) | Local development and tests. Per-container, lost on scale-down — prod logs a warning. |
| `postgres` | Anything real. Works with Supabase, Neon, RDS, or plain Postgres. |

!!! danger "Do not run Postgres on a Modal Volume"
    Modal Volumes have no distributed file locking, last-write-wins semantics,
    and changes are only durable once committed. That is unsafe for a database
    data directory — especially with autoscaling, where two containers could open
    the same files. Use a managed Postgres such as Supabase.

## Set up Supabase

1. Create a Supabase project.
2. **Project Settings → Database → Connection string → Transaction pooler**
   (port `6543`). Use the pooler, not the direct connection: Modal can run many
   containers and direct connections would exhaust Postgres' connection limit.
3. Create a Modal secret holding the URL:

   ```bash
   modal secret create supabase-db \
     PERSISTENCE_BACKEND=postgres \
     DATABASE_URL='postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres'
   ```

4. Mount it by naming it in the environment preset in `modal_common.py`:

   ```python
   DEV = EnvConfig(
       env_name="dev",
       persistence_secret_name="supabase-db",   # secret must exist before deploy
       ...
   )
   ```

5. `modal deploy`. On first start the app creates the `items` table
   (idempotent; safe when several containers start at once).

!!! warning "Secrets"
    Never commit the connection string. It lives only in the Modal secret. The
    adapter never logs it, and error responses never contain driver or
    connection details.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — (required) | Use the transaction-pooler URL |
| `DATABASE_SSL` | `require` for remote hosts | `disable` for local Docker; a `sslmode=` in the URL wins |
| `DB_POOL_MAX_SIZE` | 5 | Keep ≥ `max_concurrent_requests` so requests don't queue on the pool |
| `DB_POOL_MIN_SIZE` | 1 | |
| `DB_COMMAND_TIMEOUT_SECONDS` | 10 | |
| `EVENT_BUS_BACKEND` | `memory` | `postgres` shares streaming state across containers (below) |
| `EVENT_BUS_LISTEN_URL` | unset | Session-level URL for instant event delivery (below) |

Connections per deployment ≈ `DB_POOL_MAX_SIZE` × containers; the pooler
multiplexes them, but keep an eye on your plan's client-connection limit.

## Behaviour worth knowing

* **Name uniqueness is enforced by the database** (unique index on
  `lower(btrim(name))`), so two containers creating the same name concurrently
  produce exactly one `201` and the rest `409 ITEM_NAME_EXISTS`.
* **Outage handling:** connection-level failures return
  `503 STORAGE_UNAVAILABLE` with `Retry-After: 5`, and `/health` reports
  `unhealthy`. The pool reconnects on its own once the database is back.
* **Row-level security** is enabled on `items` with no policies. Supabase exposes
  public tables through its REST API; this locks `anon`/`authenticated` out, while
  the app (connecting as a privileged role directly) is unaffected.
* **Prepared statements are disabled** (`statement_cache_size=0`). Verified
  against PgBouncer 1.20 in transaction mode: with asyncpg's default cache the
  contract suite fails with `prepared statement "…" does not exist`; with the
  setting it passes. Modern PgBouncer (≥ 1.21) tolerates the cache, Supavisor
  and older versions do not.
* The schema is applied from `src/adapters/persistence/sql/*.sql` at startup. Teams that manage
  schema with the Supabase CLI can apply the same files with `supabase db push`
  and the startup step becomes a no-op.

## Run and test locally against Supabase

Credentials live in a local `.env` (git-ignored — only that exact name is — and
excluded from the Modal image; a test checks both). Never commit real values and
never paste the password into a chat or a ticket.

```bash
cp .env.example .env                       # then fill in the host/user from the Supabase dashboard
python scripts/set_supabase_password.py    # hidden prompt; URL-encodes the password for you
python scripts/check_supabase.py           # read-only preflight: validates, connects, checks LISTEN
python scripts/run_local.py                # serve on http://127.0.0.1:8000 using .env
```

Where the values come from (dashboard → **Connect**): the **transaction pooler**
(port 6543) is `DATABASE_URL`; the **session pooler** (same host and user, port
5432) is `EVENT_BUS_LISTEN_URL`. The user is `postgres.<project-ref>`, not `postgres`.

`scripts/check_supabase.py` tells you what is wrong and never prints the password:

| It says | Meaning |
|---|---|
| password is still a placeholder / wrapped in `[ ]` | Supabase's template shows `[YOUR-PASSWORD]` — replace the brackets too |
| `URL-encode` | the password contains `@ : / ? #`; `set_supabase_password.py` does this for you |
| user must be `postgres.<project-ref>` | you used the plain `postgres` user on a pooler |
| `LISTEN cannot work through the transaction pooler` | `EVENT_BUS_LISTEN_URL` uses port 6543 — use 5432 |
| a NOTIFY never arrived | the LISTEN URL connects but is transaction-mode; the app then polls instead |

The app protects itself too: if `EVENT_BUS_LISTEN_URL` connects but cannot receive
notifications, the bus detects it with a probe, logs a warning, and keeps fast
polling rather than silently running on the 5-second safety poll.

**Running the test suite against Supabase** — the Postgres tests **drop and
recreate** `items`, `events`, `webhook_events` and `ws_ticket_uses`, so use a
dedicated, empty project, never one with real data:

```bash
# in .env (uncomment): TEST_DATABASE_URL, TEST_LISTEN_DATABASE_URL, ALLOW_DESTRUCTIVE_DB_TESTS=yes
python -m pytest
```

pytest refuses to start against a non-local `TEST_DATABASE_URL` unless
`ALLOW_DESTRUCTIVE_DB_TESTS=yes` is set, and the checker flags a test URL that
points at the same Supabase project as `DATABASE_URL`. Without
`TEST_LISTEN_DATABASE_URL` the LISTEN tests are skipped (they need port 5432).
Only `TEST_*` and `DB_POOL_*` settings are read from `.env` by pytest — never
`DATABASE_URL` — so unit tests can never connect to Supabase by accident.

## Testing against Postgres

The same behavioural contract runs against every repository
(`tests/test_item_repository_contract.py`), including concurrent-write races.
Postgres tests are skipped unless `TEST_DATABASE_URL` is set; CI provides one.

```bash
docker run -d --name pg-test -e POSTGRES_PASSWORD=pgpass -e POSTGRES_DB=app \
  -p 55432:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql://postgres:pgpass@localhost:55432/app pytest
```

!!! warning
    The tests **drop and recreate** the `items`, `events`, `webhook_events` and
    `ws_ticket_uses` tables. Never point `TEST_DATABASE_URL` at a database with
    real data.

## Shared streaming state

`EVENT_BUS_BACKEND=postgres` moves the streaming state into the same database so
SSE, WebSocket, webhooks and tickets work across containers:

| Piece | Table | How |
|---|---|---|
| Event bus | `events` | `publish` inserts a row and `NOTIFY`s in one transaction; each container runs one pump that fans new rows out to its local streams |
| Webhook de-duplication | `webhook_events` | `INSERT … ON CONFLICT` on `(provider, webhook_id)` — one winner, even across containers |
| WebSocket ticket replay | `ws_ticket_uses` | primary key on the ticket id — one winner |

Set it in the same Modal secret (the three tables are created with `items`):

```bash
modal secret create supabase-db \
  PERSISTENCE_BACKEND=postgres \
  EVENT_BUS_BACKEND=postgres \
  DATABASE_URL='…pooler…:6543/postgres' \
  EVENT_BUS_LISTEN_URL='postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres'
```

!!! warning "`LISTEN` cannot go through the transaction pooler"
    `LISTEN/NOTIFY` needs one long-lived **session**. Supabase's transaction
    pooler (port `6543`) can't provide it. Give `EVENT_BUS_LISTEN_URL` the
    **session pooler** (port `5432` on the pooler host) or the direct connection.
    Everything else — publishing, replay, polling — works through the transaction
    pooler. Without `EVENT_BUS_LISTEN_URL` the bus still works by polling every
    `EVENT_BUS_POLL_MS`; latency is then up to that interval instead of tens of
    milliseconds. If the LISTEN connection drops, polling covers the gap and the
    connection is re-established automatically.

Guarantees and limits:

* **Ordering.** Publishers take a transaction-level advisory lock, so event ids
  are in commit order and a reader can never skip an event that commits late.
  The cost: publishing is serialised. Measured on a laptop against a local
  Docker Postgres: roughly 400–600 events/s at any concurrency, with every event
  delivered to a live subscriber. Ample for notifications and activity feeds; for
  high-volume streams use a purpose-built broker (Redis Streams, NATS, Kafka).
* **Replay** (`Last-Event-ID`) is served from the `events` table: the newest
  `EVENT_REPLAY_SIZE` events after the id. Rows older than
  `EVENT_RETENTION_SECONDS` (or beyond 100k rows) are pruned, so replay is
  best-effort for long disconnects.
* **Slow consumers** (a full 256-event queue) are disconnected rather than
  buffered, as with the memory bus; they reconnect and resume with
  `Last-Event-ID`. The pump hands events over in small chunks and yields between
  them, so a healthy consumer is never mistaken for a slow one during a burst.
* Subscribing costs one tiny query (the head of the log) so a stream starts
  exactly at "now"; it never receives older events unless it asks for replay.
* One extra connection per container for `LISTEN`, plus the pool.
