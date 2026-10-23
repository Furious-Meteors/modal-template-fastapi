# modal-template-fastapi
<p align="center">
  <img src="https://raw.githubusercontent.com/Furious-Meteors/modal-template-fastapi/production/docs/assets/images/banner.png" alt="openframe-core" width="100%" />
</p>

> A production-ready FastAPI template deployed serverlessly on Modal — Hexagonal Architecture, JWT auth with scopes, paginated CRUD, **Postgres/Supabase persistence with versioned migrations**, **SSE, WebSocket and signed-webhook streaming that works across containers**, a telemetry-sidecar pattern with OpenTelemetry and Grafana Cloud, a 681-test suite, and automated CI/CD. Measured, not guessed: see [Measured performance](#measured-performance).

[![Python](https://img.shields.io/badge/Python-3.11-4f46e5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.143-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Modal](https://img.shields.io/badge/Modal-Serverless-7c3aed?style=flat-square)](https://modal.com)
[![Postgres](https://img.shields.io/badge/Postgres-Supabase-3ecf8e?style=flat-square&logo=supabase&logoColor=white)](#persistence--migrations)
[![Tests](https://img.shields.io/badge/Tests-681%20passing-15803d?style=flat-square&logo=pytest&logoColor=white)](tests/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-enabled-f5a800?style=flat-square&logo=opentelemetry&logoColor=white)](src/infrastructure/)
[![Grafana](https://img.shields.io/badge/Grafana-Cloud-f46800?style=flat-square&logo=grafana&logoColor=white)](#observability)
[![Docs](https://img.shields.io/badge/Docs-GitHub%20Pages-4f46e5?style=flat-square&logo=materialformkdocs&logoColor=white)](https://furious-meteors.github.io/modal-template-fastapi/)
[![License](https://img.shields.io/badge/License-MIT-6b7280?style=flat-square)](LICENSE)

## Introduction

`modal-template-fastapi` is a battle-tested starting point for shipping Python microservices to the cloud. It combines the ergonomics of FastAPI with Modal's serverless platform — autoscaling, secret management and environment-aware deployments without managing infrastructure.

The template is built on **Hexagonal Architecture (Ports & Adapters)** so every layer — persistence, event bus, telemetry, HTTP — can be swapped independently without touching business logic. Clone it, drop in your domain, wire your adapter, and ship.

**Key features:**

- 🏛️ **Hexagonal Architecture** — pure `core/` (domain, ports, services), concrete `adapters/`, one wiring point in `deps.py`
- 🗄️ **Postgres / Supabase persistence** — asyncpg pool behind a transaction pooler, database-enforced name uniqueness, clean `503` on outages
- 🧬 **Versioned schema migrations** — applied once, atomic, concurrency-safe, checksum-protected, adopts existing databases, `AUTO_MIGRATE=false` + CLI for deploy-step migrations
- 📡 **Streaming that works across containers** — Server-Sent Events, WebSockets (ticket auth) and signed webhooks on a shared `EventBus` (Postgres `LISTEN/NOTIFY` + replay)
- 🔒 **JWT auth + scopes** — `require_scope(...)`, topic scopes for streams, least-privilege WebSocket tickets, HMAC-verified webhooks
- 🛡️ **Abuse protection** — per-user / per-provider / per-connection rate limits, a connection cap that cannot starve normal requests, bounded request bodies
- 🔭 **Telemetry sidecars** — `TelemetryMiddleware` (HTTP + WebSocket), `TracingProxy`, `MeteredItemService`/`MeteredEventBus`; `core/` and `api/` contain **no** telemetry code (a test enforces it)
- 📊 **OpenTelemetry + Grafana Cloud** — traces, HTTP / stream / webhook / business metrics, per-container instance ids
- ☁️ **Modal serverless** — `feat` / `dev` / `prod` via `EnvConfig`, container caps, memory snapshots, `prod` refuses to start on in-memory storage
- 🧪 **681-test suite** — contract tests run against every backend, round-trip budgets, mutation-verified guards; a repeatable [smoke test](docs/smoke-test.md) for any deployment
- 🔌 **Platform-decoupled** — `src/` has zero Modal imports; runs on any ASGI host unchanged

---

## Measured performance

Everything below was measured on the real stack unless marked otherwise: a Modal container in `us-east` talking to Supabase PostgreSQL 17 in `us-east-2` (Ohio) through the transaction pooler. Server-side numbers come from the app's own request log (so the client's distance is excluded). **The single biggest lever is putting the database in the same region as the app.**

### Request latency (server-side, median)

| Request | DB in Seoul, original code | DB in Ohio, original code | **DB in Ohio, current code** | p95 (current) |
|---|---|---|---|---|
| `GET /items` (list) | 1245 ms | 147 ms | **64 ms** | 185 ms |
| `POST /items` (create) | 2083 ms | 323 ms | **133 ms** | 425 ms |
| `DELETE /items/{id}` | – | – | **133 ms** | 369 ms |
| `GET /health` (one DB read) | 940 ms | 258 ms | – | – |

Moving the database next to the app cut times **3.6–8.5×**; cutting database round trips cut them a further **2.3–2.4×**. A 4-minute soak on the live deployment served 903 of 903 requests with no errors and no tracebacks.

### Database round trips per operation

Each database call is a network round trip, and the cost shows. A hot path may not exceed its budget — `tests/test_round_trips.py` fails if it does.

| Operation | Calls before | **Calls now** |
|---|---|---|
| `get` / `list` | 1 / 4 | **1 / 1** |
| `create` / `update` / `delete` | ~7 / ~8 / ~6 | **2 / 2 / 2** (the write + one `publish_event()`) |

Behind a transaction pooler asyncpg cannot cache prepared statements, so each parameterised call is two wire round trips: one `select 1` through the pooler measured **74 ms** (≈ 37 ms per wire round trip, Modal `us-east` → Ohio).

### Throughput vs. pool size (30 ms round trip, 5 simultaneous clients)

Measured locally with a delay proxy standing in for network distance; 5 clients = one container's `max_concurrent_requests`.

| `DB_POOL_MAX_SIZE` | list — before → now | create — before → now |
|---|---|---|
| 1 | 4.4 → **9.4** req/s | 2.2 → **5.0** req/s |
| 2 | 8.4 → **19.1** req/s | 4.0 → **9.6** req/s |
| 5 (default) | 20.8 → **46.2** req/s (median 238 → **105 ms**) | 6.1 → **23.6** req/s (median 857 → **208 ms**) |

A pool of 2 now does what a pool of 5 did. A container holds **1–6** connections: the pool (1 at start, up to `DB_POOL_MAX_SIZE`) plus one optional `LISTEN` connection.

### Events, streams and cold starts

| Measurement | Value | Conditions |
|---|---|---|
| Event publish throughput | **≈ 4,800 events/s** | database next to the app, 20 publishers |
| … at a 30 ms round trip | **47/s** with 5 connections, **191/s** with 20 | delay proxy; was ~6/s before `publish_event()` became one statement |
| Cross-container event delivery | **41 ms** with `LISTEN/NOTIFY`, **405 ms** polling at 1 s | local Docker, pooler + direct-connection topology |
| Write → event on a live SSE stream | **≈ 0.4 s** | measured from a client in India, includes the write itself |
| Slot freed after a client disconnects | **0.7 s** (was ~11 s) | behind Modal's proxy; heartbeat 5 s (was 15 s) |
| First request after a deploy / scale-from-zero | **≈ 9–11 s** | includes ~1 s of client distance |

### Why there is no shared "database gateway" container

A gateway holding a few connections for all containers was measured and rejected: the extra RPC hop costs **~31 ms per operation** (a direct query is 74 ms, via the gateway 101 ms), throughput fell **37 %** (55.7 → 34.9 ops/s), and every app container would share one fixed connection ceiling. Supabase's pooler already multiplexes connections. The round-trip work above is what actually frees connections.

---

## Project Structure

```
modal-template-fastapi/
├── src/
│   ├── main.py                        # FastAPI app, CORS, lifespan (migrate → wire → serve)
│   ├── deps.py                        # Wiring: adapters → services, sidecars, startup validation
│   ├── core/                          # Pure business logic — zero I/O or telemetry imports
│   │   ├── domain/                    # item.py, events.py, errors.py
│   │   ├── ports/                     # ItemRepository, EventBus, WebhookEventStore, TicketReplayStore, RateLimiter, TokenVerifier
│   │   └── services/item_service.py   # Business logic; publishes item.created/updated/deleted
│   ├── adapters/                      # Concrete implementations — know about core, not HTTP routes
│   │   ├── http/middleware.py         # TelemetryMiddleware — pure ASGI: HTTP + WebSocket + stream/webhook metrics
│   │   ├── persistence/
│   │   │   ├── memory.py              # InMemoryItemRepository (dev/tests; enforces name uniqueness)
│   │   │   ├── postgres.py            # PostgresItemRepository + pool (Supabase-ready)
│   │   │   ├── migrations.py          # Versioned migration runner
│   │   │   └── sql/                   # 001–004 versioned, R__publish_event_lockdown repeatable
│   │   ├── auth/jwt.py                # TokenVerifier: shared secret (HS*) or identity-provider JWKS (RS*/ES*)
│   │   ├── ratelimit/postgres.py      # RateLimiter shared by every container (fixed window)
│   │   ├── events/
│   │   │   ├── memory.py              # In-memory bus, webhook store, ticket store
│   │   │   └── postgres.py            # Postgres bus (events table + LISTEN/NOTIFY), webhook + ticket stores
│   │   └── telemetry/                 # tracing_proxy.py, metered.py (business metrics), stream_metrics.py
│   ├── api/                           # HTTP layer — no telemetry code
│   │   ├── routes.py                  # Items + health
│   │   ├── events.py                  # SSE stream, WebSocket ticket + stream
│   │   ├── webhooks.py                # HMAC-verified webhook ingestion
│   │   ├── auth.py                    # JWT + scopes + WebSocket tickets
│   │   ├── event_access.py            # Topic scopes + per-event visibility hook
│   │   ├── ratelimit.py, streaming.py # Token buckets; connection cap + heartbeat helper
│   │   ├── errors.py, models.py       # Unified errors; Pydantic schemas
│   ├── infrastructure/                # OTel bootstrap (setup.py) + business metric instruments (metrics.py)
│   └── utils/                         # config.py (env), dburl.py (safe URL checks), envfile.py (.env reader)
├── scripts/                           # run_local, check_supabase, set_supabase_password, migrate,
│                                      # push_modal_secret, modal_mint_token, smoke_test, jwt_token_generator
├── tests/                             # 681 tests (see Testing)
├── modal_app.py                       # Modal Cls, @enter telemetry init, ASGI wrapper
├── modal_common.py                    # EnvConfig, pinned image, environment presets
├── .env.example                       # Template for local Supabase settings (copy to .env — git-ignored)
├── docs/                              # MkDocs site: architecture, persistence, streaming, smoke test, CI/CD
└── .github/workflows/                 # app-testing (with a Postgres service), modal-deploy, docs
```

---

## Architecture

The dependency rule is strict — everything points inward toward `core/`. Adapters know about `core/`; `core/` never knows about adapters or telemetry.

```
                        src/infrastructure/  — OTel SDK setup, business metric instruments
                                    │ get_tracer() / get_meter()
   ┌────────────────────────────────▼───────────────────────────────────────────────┐
   │ src/api/   routes · SSE · WebSocket · webhooks · auth · rate limits  (no OTel)  │
   └────────────────────────────────┬───────────────────────────────────────────────┘
   ┌────────────────────────────────▼───────────────────────────────────────────────┐
   │ src/adapters/                                                                  │
   │  http/middleware.py   TelemetryMiddleware ── HTTP · WebSocket · stream · webhook│
   │  telemetry/           TracingProxy · MeteredItemService · MeteredEventBus       │
   │  persistence/         memory │ postgres + migrations/sql                          │
   │  events/              memory │ postgres (events table + LISTEN/NOTIFY)           │
   └────────────────────────────────┬───────────────────────────────────────────────┘
        ItemRepository · EventBus · WebhookEventStore · TicketReplayStore  (ports)
   ┌────────────────────────────────▼───────────────────────────────────────────────┐
   │ src/core/   domain (item, events) · ports · services/item_service  — pure Python│
   └────────────────────────────────────────────────────────────────────────────────┘
```

**Across containers:**

```
 Modal containers (each: API + event pump)
        │ queries ─────────────► Supabase transaction pooler :6543 ──► Postgres
        │ LISTEN (optional) ───► Supabase session pooler     :5432 ──► Postgres
        └─ every container sees every event via the shared `events` table
```

**Swapping persistence or the event bus** (e.g. Redis) needs one adapter, one branch in `deps.py`, and an env var — no routes, services, domain or telemetry files change. The shared contract tests (`test_item_repository_contract.py`, `test_event_bus.py`, `test_shared_state_contract.py`) hold every backend to the same behaviour.

---

## Local Development

```bash
# 1. Install test dependencies (pinned to the same versions as the deploy image)
pip install -r .github/requirements/test.txt

# 2. Run the suite — no database needed (531 tests run; 98 Postgres tests skip)
pytest

# 3. Include the Postgres tests (all 681): a disposable database only — they DROP tables
docker run -d --name pg -e POSTGRES_PASSWORD=pgpass -e POSTGRES_DB=app -p 55432:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql://postgres:pgpass@localhost:55432/app pytest

# 4. Run the API against Supabase/Postgres using a local, git-ignored .env
cp .env.example .env                       # fill in host/user from the Supabase dashboard
python scripts/set_supabase_password.py    # hidden prompt; URL-encodes the password
python scripts/check_supabase.py           # read-only preflight: validates, connects, checks LISTEN, shows migrations
python scripts/run_local.py                # http://127.0.0.1:8000

# 5. Verify it end to end
python scripts/smoke_test.py --url http://127.0.0.1:8000 --jwt-secret "$JWT_SECRET" --webhook-secret "$WEBHOOK_SECRET_ACME"
```

`.env` is git-ignored and excluded from the Modal image (a test checks both); pytest only reads `TEST_*` settings from it, never `DATABASE_URL`, and refuses a non-local `TEST_DATABASE_URL` unless `ALLOW_DESTRUCTIVE_DB_TESTS=yes`. `scripts/check_supabase.py` explains the usual mistakes (unreplaced `[YOUR-PASSWORD]`, the plain `postgres` user on a pooler, `LISTEN` through port 6543) without ever printing the password.

---

## Deployment

```bash
# 1. Create the per-environment Modal secrets (secrets are scoped per Modal environment!)
modal secret create fastapi-auth-secrets --env feat JWT_SECRET=<32+ chars> JWT_ALGORITHM=HS256
python scripts/push_modal_secret.py --environment feat                                   # supabase-db, from .env
python scripts/push_modal_secret.py --environment feat --name webhook-secrets --keys WEBHOOK_SECRET_ACME

# 2. Deploy
MODAL_ENV=feat modal deploy modal_app.py --env feat

# 3. Verify the deployment (mints short-lived tokens inside Modal; the JWT secret never leaves it)
modal run --env feat scripts/modal_mint_token.py --out /tmp/tokens.json
python scripts/smoke_test.py --url https://<app>.modal.run --yes --token ... --limited-token ... --webhook-secret ...
```

Or push a branch — CI/CD handles it: pushes to `feat/**` and merges to `dev`/`production` run the full suite (with a Postgres service), then `modal deploy` to the matching environment. Failing tests block the deploy.

| Environment | Containers (max) | Telemetry | Notes |
|---|---|---|---|
| `feat` | 3 | on | verified live end to end |
| `dev` | 3 | off (free) | |
| `prod` | 5 | on | **refuses to start on in-memory storage** unless `ALLOW_MEMORY_IN_PROD=true` |

Every preset mounts the same secrets (`fastapi-auth-secrets`, `supabase-db`, `webhook-secrets`; `grafana-otlp` where telemetry is on). `modal deploy` fails if a mounted secret is missing, so create them per environment first. Container caps bound database connections: containers × (pool of 5 + 1 `LISTEN`) must stay under your Supabase plan's pooler limit.

> **Keep Supabase in the same region as Modal.** Modal's default is `us-east`; a project in `us-east-2` measured 3.6–8.5× faster than one in Seoul (see [Measured performance](#measured-performance)). Supabase cannot move an existing project; create a new one and re-run `push_modal_secret.py --replace`.

> See the [CI/CD guide](https://your-org.github.io/modal-template-fastapi/cicd/), [Deployment guide](https://your-org.github.io/modal-template-fastapi/deployment/) and [Persistence guide](https://your-org.github.io/modal-template-fastapi/persistence/).

---

## Persistence & Migrations

`PERSISTENCE_BACKEND` and `EVENT_BUS_BACKEND` are `memory` (dev/tests; per container, lost on scale-down) or `postgres` (shared across containers). Postgres tables: `items`, `events`, `webhook_events`, `ws_ticket_uses`, `schema_migrations` — all with row-level security on so Supabase's REST API cannot reach them.

**Schema migrations** live in `src/adapters/persistence/sql/`:

| File | Kind | Behaviour |
|---|---|---|
| `NNN_name.sql` | versioned | applied **once**, in order, recorded with a sha256; **immutable** afterwards |
| `R__name.sql` | repeatable | re-run on **every** start (grants, revokes, views) — must be idempotent |

To change the schema, add the next numbered file — editing an applied one makes startup refuse to continue. The batch runs in one transaction under an advisory lock: containers starting together apply each migration exactly once, and a failing migration rolls everything back. Existing databases are adopted without touching their data; a database *newer* than the code is tolerated so rollbacks are never blocked.

```bash
python scripts/migrate.py status --check   # exit 1 if anything is pending or edited — a CI gate
python scripts/migrate.py up               # apply as a deploy step, with AUTO_MIGRATE=false on the app
```

Migrations live in the repository, not on a Modal Volume, because they are code that must match the app version that runs them (no version skew, review, CI, rollback-by-redeploy). Full reasoning and the rolling-deploy rules: [Persistence guide](https://your-org.github.io/modal-template-fastapi/persistence/).

---

## Streaming & Webhooks

All three transports share one `EventBus` port; with `EVENT_BUS_BACKEND=postgres` a stream on any container sees events from every container.

| | Endpoint | Auth | Notes |
|---|---|---|---|
| **SSE** | `GET /api/v1/events/stream?topic=` | JWT | heartbeat 5 s, `Last-Event-ID` resume (last 100 events), ends itself before Modal's timeout |
| **WebSocket** | `POST /ws/ticket` → `WS /ws?ticket=` | single-use ticket (30 s) | ping/pong, 4 KB message limit, documented close codes |
| **Webhooks** | `POST /webhooks/{provider}` | HMAC-SHA256 | timestamp tolerance ±300 s, idempotency, 1 MiB limit enforced while reading, `202` fast |

- **Topic scopes:** subscribing needs `events:read` **plus** the topic's scope (`items:read` / `webhooks:read`), so a stream never reveals what the REST API would deny. `event_access.can_view()` is a per-event hook for ownership rules.
- **Tickets** are signed JWTs with their own audience, carry only *read* scopes, and are single-use across containers (shared replay store).
- **Webhook checks run in a fixed order** — provider → rate limit → size → timestamp → signature → id → payload → duplicate — and nothing is parsed or stored until the signature verifies.
- **Rate limits** (per container, never per IP): tickets 30/min per user, webhooks 1200/min per provider, WebSocket messages 10/s per connection.
- **Connection cap:** `max_streams` (3 per container) stays below `max_concurrent_requests` so streams can never starve normal requests. Behind Modal's proxy the app learns of a disconnect only when a heartbeat write fails, so a closed stream frees its slot within one heartbeat; reconnecting clients should honour `Retry-After` on `503`.

Details, close codes and examples: [Streaming & Webhooks](https://your-org.github.io/modal-template-fastapi/api/streaming/).

---

## API Reference

| Method | Endpoint | Auth | Scope | Description |
|---|---|---|---|---|
| `GET` | `/api/v1/health` | None | — | Health check — performs a real database read (`503` + `unhealthy` if it fails) |
| `GET` | `/api/v1/items` | JWT | `items:read` | List items (paginated) |
| `GET` | `/api/v1/items/{id}` | JWT | `items:read` | Get a single item |
| `POST` | `/api/v1/items` | JWT | `items:write` | Create an item |
| `PUT` | `/api/v1/items/{id}` | JWT | `items:write` | Replace an item (omitted optional fields are cleared) |
| `DELETE` | `/api/v1/items/{id}` | JWT | `items:write` | Delete an item |
| `GET` | `/api/v1/events/stream` | JWT | `events:read` + topic scope | Server-Sent Events (`?topic=items\|webhooks`) |
| `POST` | `/api/v1/ws/ticket` | JWT | `events:read` | Exchange a JWT for a single-use WebSocket ticket |
| `WS` | `/api/v1/ws?ticket=…` | Ticket | `events:read` + topic scope | WebSocket event stream |
| `POST` | `/api/v1/webhooks/{provider}` | HMAC | — | Receive a signed webhook (`WEBHOOK_SECRET_<PROVIDER>`) |

**Unique names:** item names are unique across the service, compared ignoring case and surrounding whitespace, and trimmed on input (blank names are rejected). `POST` and `PUT` return `409 ITEM_NAME_EXISTS` for a duplicate; an item may keep its own name on update. The database enforces it with a unique index, so concurrent creates across containers produce exactly one winner (verified live: 40 simultaneous creates → one `201`, 39 `409`).

**Paginated list response:**
```json
{ "items": [...], "total": 42, "limit": 10, "offset": 0, "has_more": true }
```

> Full endpoint reference with curl examples and Swagger UI: [API docs →](https://your-org.github.io/modal-template-fastapi/api/endpoints/)

---

## Authentication & Authorisation

**JWT Bearer tokens** with scope-based authorisation. Tokens must carry `exp` and `sub`; `JWT_ALGORITHM` is limited to `HS256`/`HS384`/`HS512`; with `AUTH_BACKEND=jwks` tokens are verified against your identity provider's published keys instead (see [Swapping a layer](#swapping-a-layer)); set `JWT_AUDIENCE` / `JWT_ISSUER` to also enforce `aud` / `iss`. `JWT_SECRET` must be at least 32 characters and is validated at startup, so a bad config stops the container instead of failing the first request.

```python
current_user: TokenPayload = Depends(require_scope("items:read"))    # reads
current_user: TokenPayload = Depends(require_scope("items:write"))   # writes
```

| Scope | Grants |
|---|---|
| `items:read` / `items:write` | read / modify items |
| `events:read` | open a stream or request a WebSocket ticket |
| `webhooks:read` | read the `webhooks` stream topic (third-party payloads — grant deliberately) |

```json
{ "sub": "user@example.com", "exp": 1234567890, "scopes": ["items:read", "items:write", "events:read"] }
```

Generate a token for testing: `python scripts/jwt_token_generator.py --secret <JWT_SECRET>`. For a Modal deployment whose secret you cannot read, `scripts/modal_mint_token.py` mints short-lived tokens inside Modal.

### Errors

Every error is the same flat body — `{"detail", "session_id", "error_code"}` plus an `errors` list of `{field, message, type}` for validation failures. `session_id` always matches the `x-session-id` response header.

| Status | Codes |
|---|---|
| 400 | `MISSING_TIMESTAMP`, `INVALID_TIMESTAMP`, `STALE_TIMESTAMP`, `MISSING_WEBHOOK_ID`, `INVALID_PAYLOAD` |
| 401 | `MISSING_TOKEN`, `TOKEN_EXPIRED`, `INVALID_TOKEN`, `INVALID_TICKET`, `TICKET_EXPIRED`, `MISSING_SIGNATURE`, `INVALID_SIGNATURE` |
| 403 | `INSUFFICIENT_SCOPE` |
| 404 | `ITEM_NOT_FOUND`, `NOT_FOUND`, `UNKNOWN_WEBHOOK_PROVIDER` |
| 405 / 409 / 413 | `METHOD_NOT_ALLOWED` / `ITEM_NAME_EXISTS` / `PAYLOAD_TOO_LARGE` |
| 422 / 429 | `VALIDATION_ERROR` / `RATE_LIMITED` (with `Retry-After`) |
| 500 / 503 | `INTERNAL_ERROR` / `STORAGE_UNAVAILABLE`, `STREAM_LIMIT_REACHED` (both with `Retry-After`) |

---

## Observability

Telemetry is a **sidecar** — `core/` and `api/` have zero OTel knowledge (enforced by tests). Four sidecars cover everything:

| Sidecar | Where | What it adds |
|---|---|---|
| `TelemetryMiddleware` | HTTP + WebSocket boundary | Request / connection span, duration, status, session ID, **stream and webhook metrics derived from the wire** |
| `TracingProxy` | Port boundary | Child span per repository, event-bus, webhook-store and ticket-store call |
| `MeteredItemService` | Service boundary | Item operation outcomes and durations |
| `MeteredEventBus` | Bus boundary | Publish outcomes/durations, delivery lag, dropped consumers |

| Signal | Detail |
|---|---|
| Distributed trace | Span per request and per WebSocket connection — route template, status, session ID, close code |
| HTTP | `http.server.request.count` / `.duration` (ms) / `.error.count` / `.active_requests` / `.response.size` |
| Item operations | `app.items.operations` by `operation` and `outcome` (`ok`/`not_found`/`conflict`/`unavailable`/`error`); `app.items.operation.duration` |
| Events | `app.events.published`, `app.events.publish.duration`, `app.events.delivery.lag` (event creation → delivery to a consumer), `app.events.replayed`, `app.events.subscriptions.dropped` |
| Streams / webhooks | `stream.connections.active/closed` (with close reason), `stream.messages.sent`, `webhook.received` (with outcome; `provider` labelled only for configured providers) |
| Cold starts | `app.container.cold_start.count` |
| Correlation | `otelTraceID` / `otelSpanID` on every log line; W3C `traceparent` propagation |

Label values are small fixed sets — never ids, names or users. Metrics carry a per-container `service.instance.id` (Modal task id) so containers' cumulative counters never overwrite each other, and export every 60 s (`metric_export_interval_seconds`). FastAPI's own request telemetry is off by default (`FASTAPI_NATIVE_TELEMETRY`) so signals aren't doubled.

> **Grafana's OTLP gateway occasionally answers `429 Too Many Requests`** to metric batches (about 1 export in 5 in a 17-minute test with 3 containers, even at one request/minute; not reproducible on demand). The SDK retries, and counters/histograms are cumulative, so a rejected batch loses one data point, not data. If it matters, check the stack's usage/limits in Grafana Cloud.

**Adding a business metric:** define the instrument and a `record_*` method in `src/infrastructure/metrics.py`, call it from the relevant sidecar in `src/adapters/telemetry/metered.py`, add a test. Never from `core/` or a route.

**Environment strategy:** `feat` and `prod` push telemetry; `dev` is silent (zero cost). Controlled by `EnvConfig.telemetry_enabled`.

```bash
modal secret create grafana-otlp --env <env> \
  GRAFANA_INSTANCE_ID=<numeric stack ID> \
  GRAFANA_OTLP_TOKEN=<API token with metrics:write traces:write> \
  GRAFANA_OTLP_ENDPOINT=<https://otlp-gateway-<region>.grafana.net/otlp>
```

---

## Configuration reference

Set through `EnvConfig` in `modal_common.py` (which exports them) or directly as env vars / Modal secrets.

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_BACKEND` | `secret` | `secret` (shared HS* secret) or `jwks` (identity provider keys) |
| `JWT_SECRET` · `JWT_ALGORITHM` · `JWT_AUDIENCE` · `JWT_ISSUER` | — · `HS256` (`RS256` with jwks) · — · — | Token verification |
| `JWT_JWKS_URL` · `JWT_SCOPES_CLAIM` · `WS_TICKET_SECRET` | — · `scopes` (falls back to OAuth2 `scope`) · `JWT_SECRET` | `jwks` mode: key URL, claim holding scopes, secret that signs WebSocket tickets (required with jwks) |
| `PERSISTENCE_BACKEND` · `EVENT_BUS_BACKEND` | `memory` | `memory` or `postgres` |
| `DATABASE_URL` · `EVENT_BUS_LISTEN_URL` | — | Transaction-pooler URL · session-level URL for instant events (optional) |
| `DATABASE_MODE` | `pooler` | `pooler` (transaction pooler, statement cache off) or `direct` (cache on: one wire round trip per query instead of two) |
| `DATABASE_SSL` · `DB_POOL_MAX_SIZE` · `DB_POOL_MIN_SIZE` · `DB_COMMAND_TIMEOUT_SECONDS` | `require` (remote) · 5 · 1 · 10 | Connection pool |
| `AUTO_MIGRATE` · `MIGRATION_LOCK_TIMEOUT_SECONDS` · `MIGRATION_STATEMENT_TIMEOUT_SECONDS` | `true` · 120 · 300 | Schema migrations |
| `EVENT_BUS_POLL_MS` · `EVENT_RETENTION_SECONDS` · `EVENT_REPLAY_SIZE` | 1000 · 3600 · 100 | Postgres event bus |
| `STREAM_MAX_CONNECTIONS` · `STREAM_HEARTBEAT_SECONDS` · `STREAM_MAX_DURATION_SECONDS` | 3 · 5 · 120 (Modal) | Streams (derived from `EnvConfig`) |
| `WS_MAX_MESSAGE_BYTES` · `WS_TICKET_TTL_SECONDS` | 4096 · 30 | WebSocket |
| `WEBHOOK_SECRET_<PROVIDER>` · `WEBHOOK_TOLERANCE_SECONDS` · `WEBHOOK_MAX_BODY_BYTES` · `WEBHOOK_IDEMPOTENCY_TTL_SECONDS` | — · 300 · 1 MiB · 86400 | Webhooks |
| `RATE_LIMIT_BACKEND` | `memory` | `memory` (per container) or `postgres` (one counter shared by every container) |
| `RATE_TICKET_PER_MINUTE` · `RATE_WEBHOOK_PER_MINUTE` · `RATE_WS_MESSAGES_PER_SECOND` | 30 · 1200 · 10 | Rate limits (WebSocket messages are always per connection) |
| `ALLOW_MEMORY_IN_PROD` | unset | Opt in to in-memory storage in `prod` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` · `OTEL_EXPORTER_OTLP_HEADERS` | — | Any OTLP/HTTP backend (`GRAFANA_INSTANCE_ID` + `GRAFANA_OTLP_TOKEN` is a Grafana shortcut for the headers) |
| `OTEL_METRIC_EXPORT_INTERVAL_MS` · `FASTAPI_NATIVE_TELEMETRY` | 60000 (Modal) · off | Telemetry |
| `APP_ENV` · `INSTANCE_ID` | `MODAL_ENV` · `MODAL_TASK_ID`/hostname | Environment name and container id on any host |
| `TEST_DATABASE_URL` · `TEST_LISTEN_DATABASE_URL` · `ALLOW_DESTRUCTIVE_DB_TESTS` | — | Postgres tests (drop tables!) |

---

## Testing

**681 tests** (about 14 s without a database; about 25 s with Postgres), on Python 3.11 and 3.14.

- **Contract tests** run the same behaviour against every backend (item repository, event bus, webhook and ticket stores): ordering, replay, slow-consumer drop, uniqueness races.
- **Concurrency tests** use several independent bus/pool instances standing in for containers.
- **Round-trip budgets** (`test_round_trips.py`) count database calls per operation.
- **Migration tests** cover adoption, evolution with data kept, atomic rollback, 10 concurrent starters, drift, timeouts and the CLI.
- **Guards were verified by breaking them**: the unique index, the ordering lock, the `LISTEN` probe, the drift check and the image's `.env` exclusion were each removed in turn to confirm a test fails.
- **Smoke test** (`scripts/smoke_test.py`, 28 checks) runs against any deployment; it passes against the live `feat` environment.

---

## Adapting This Template

### Replace the domain

1. Edit `src/core/domain/item.py` — add your typed fields, update `to_dict()`
2. Edit `src/core/services/item_service.py` — adjust business logic
3. Add a numbered migration for any schema change (`src/adapters/persistence/sql/004_….sql`)
4. Edit `src/api/models.py` and `src/api/routes.py`

### Swapping a layer

Every capability sits behind a small port in `src/core/ports/`. A swap is one adapter, one line in `src/deps.py`, and an environment variable; nothing in `core/` or `api/` changes, and the contract tests every backend must pass are already written.

| Layer | Port | Adapters today | Switch with | To add another |
|---|---|---|---|---|
| Item storage | `ItemRepository` | memory, Postgres | `PERSISTENCE_BACKEND` | adapter in `adapters/persistence/`, branch in `_get_repository`, add to `tests/test_item_repository_contract.py` |
| Event bus | `EventBus` | memory, Postgres (`LISTEN/NOTIFY`) | `EVENT_BUS_BACKEND` | adapter in `adapters/events/`; must keep per-topic order and replay (`test_event_bus.py`) |
| Webhook / ticket state | `WebhookEventStore`, `TicketReplayStore` | memory, Postgres | follows `EVENT_BUS_BACKEND` | needs an atomic "first one wins" (`test_shared_state_contract.py`) |
| Rate limiting | `RateLimiter` | per-container token bucket, Postgres fixed window | `RATE_LIMIT_BACKEND` | adapter in `adapters/ratelimit/`, branch in `_install_rate_limiters` (Redis `INCR`+`EXPIRE` fits) |
| Authentication | `TokenVerifier` | shared secret (HS*), JWKS (RS*/ES*/PS*) | `AUTH_BACKEND` | adapter in `adapters/auth/`, branch in `get_token_verifier` |
| Telemetry | none: OpenTelemetry is the port | any OTLP/HTTP backend | `OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_EXPORTER_OTLP_HEADERS` | nothing to write |
| Hosting | `src.main:app` (plain ASGI) | Modal (`modal_app.py`, `modal_common.py`) | which entrypoint you run | `uvicorn src.main:app`, then re-derive the stream numbers below |

**Identity provider (Auth0, Cognito, Clerk, Keycloak, Supabase Auth, ...)**: set `AUTH_BACKEND=jwks`, `JWT_JWKS_URL`, `JWT_ALGORITHM=RS256` (or `ES256`), `JWT_AUDIENCE`, `JWT_ISSUER` and `WS_TICKET_SECRET` (WebSocket tickets are issued by this service, so they never depend on the provider). If your provider puts scopes in a differently named claim, set `JWT_SCOPES_CLAIM`; the OAuth2 `scope` string (`"a b c"`) works without it. Keys are cached and re-fetched when a token names an unknown `kid`, at most once per 30 s (PyJWT's cooldown against forged key ids), so rotation needs no redeploy. Without `JWT_AUDIENCE` a token your provider issued for *any* app is accepted; startup warns about it.

**Database connections**: `DATABASE_MODE=direct` turns the prepared-statement cache back on (one wire round trip per query, about half the latency) and lets `LISTEN` reuse `DATABASE_URL`. Only use it when `max_containers × (pool + 1)` fits the database's real connection limit; set `database_connection_limit` on the `EnvConfig` preset and the config refuses a scale-out that would not.

**Moving off Modal**: the application code has no Modal imports. What is Modal-specific is tuning: `STREAM_HEARTBEAT_SECONDS` (5 s) and `STREAM_MAX_CONNECTIONS` exist because Modal's proxy does not report client disconnects, and `STREAM_MAX_DURATION_SECONDS` sits under Modal's 150 s request limit. A platform that reports disconnects and has longer timeouts can raise all three. Set `APP_ENV` and `INSTANCE_ID` instead of `MODAL_ENV` / `MODAL_TASK_ID`.

The shared rate limiter (`RATE_LIMIT_BACKEND=postgres`) is a fixed-window counter: one database round trip per check, no burst allowance, and it fails open if the database is unreachable. Keep the per-container limiter for abuse protection; use the shared one when the limit is a real quota.

### Add a new service

1. Add domain entity + port + service under `src/core/`; an adapter under `src/adapters/`
2. Add a `get_<service>()` dependency in `src/deps.py`; wire into routes with `Depends(...)`
3. Telemetry: `TracingProxy` covers any new port; add a `Metered…` sidecar for business metrics

---

## CORS Configuration

CORS origins are configured per environment in `modal_common.py`:

```python
FEAT = EnvConfig(cors_origins=["*"])   # replace with your Modal domain
PROD = EnvConfig(cors_origins=["*"])   # replace with your Modal domain
```

Replace `["*"]` with your real origins before production (`prod` logs a warning while it is `*`). Credentialed CORS is only enabled for explicit origins. The value propagates through `configure_env_vars` → `CORS_ORIGINS` → `src/utils/config.py`.

---

## Known limitations

Documented, not hidden:

- **CORS is `*` in every preset** until you set your origins.
- **Rate limits and the stream cap are per container by default** — with N containers the effective limit is up to N×. Set `RATE_LIMIT_BACKEND=postgres` for a limit shared by every container (costs one database round trip per check), or enforce hard global quotas at the edge. The stream cap is always per container.
- **Event publishing is serialised** by a database-wide lock (≈ 4,800/s next to the database, 47–191/s at a 30 ms round trip). Use a broker (Redis Streams, NATS, Kafka) for very high volume.
- **Auth is HS256-family only**, with no token issuing or secret-rotation procedure; stream authorisation defaults to "every subscriber sees every event on a topic they may read" (override `can_view`).
- **Migrations are forward-only and transactional** — no `CREATE INDEX CONCURRENTLY`; run those by hand.
- **Behind a transaction pooler each parameterised query is two wire round trips** (statement cache must be off). Keep the database in the app's region.
- **Grafana's gateway may rate-limit metric batches** (see Observability); no data is lost.
- **Only `items` is a persisted entity**; the domain is a placeholder for yours.

---

## Documentation

| Section | Contents |
|---|---|
| [Architecture](https://your-org.github.io/modal-template-fastapi/architecture/) | Component diagrams, request lifecycle, module reference |
| [API Reference](https://your-org.github.io/modal-template-fastapi/api/endpoints/) | Endpoints, curl examples, pagination, Swagger UI |
| [Streaming & Webhooks](https://your-org.github.io/modal-template-fastapi/api/streaming/) | SSE, WebSocket tickets, signed webhooks, rate limits, close codes, metrics |
| [Persistence](https://your-org.github.io/modal-template-fastapi/persistence/) | Postgres/Supabase setup, migrations, connections per container, local testing, Modal secrets |
| [Smoke test](https://your-org.github.io/modal-template-fastapi/smoke-test/) | Verify any deployment end to end |
| [Authentication](https://your-org.github.io/modal-template-fastapi/api/auth/) | JWT flow, scopes, error codes, token generation |
| [Testing](https://your-org.github.io/modal-template-fastapi/testing/) | Fixture reference, test matrix, isolation strategy |
| [CI/CD](https://your-org.github.io/modal-template-fastapi/cicd/) | Workflow diagrams, trigger matrix, required secrets |
| [Deployment](https://your-org.github.io/modal-template-fastapi/deployment/) | EnvConfig reference, environment matrix, operational runbook |

---

## Contributing

Contributions are welcome. Please follow these steps:

1. Fork the repository and create a branch from `dev`
2. Make your changes — add tests for any new behaviour (a new backend must pass the contract tests; a schema change needs a new numbered migration)
3. Run `pytest` (and the Postgres tests with `TEST_DATABASE_URL`) and confirm everything passes
4. Open a pull request targeting `dev` with a clear description of the change

For significant changes, open an issue first to discuss the approach.

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.
