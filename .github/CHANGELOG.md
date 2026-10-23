# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Types of changes**
> `Added` · `Changed` · `Deprecated` · `Removed` · `Fixed` · `Security`

---

## [Unreleased]

### Added (swappable layers)
- **Authentication is a port** (`TokenVerifier`, `src/adapters/auth/jwt.py`). `AUTH_BACKEND=secret` (default, unchanged) or `jwks`: verify RS*/ES*/PS* tokens against an identity provider's published keys (`JWT_JWKS_URL`), with key caching, `kid`-based rotation (PyJWT refetches on an unknown key id, at most every 30 s), audience/issuer enforcement, and the OAuth2 space-delimited `scope` claim (or any claim via `JWT_SCOPES_CLAIM`). Startup validates the configuration and warns when `JWT_AUDIENCE` is unset. WebSocket tickets are signed with `WS_TICKET_SECRET` (default `JWT_SECRET`) so they never depend on the provider. Adds `cryptography` to the pinned image
- **Rate limiting is a port** (`RateLimiter`). `RATE_LIMIT_BACKEND=memory` (default, per container) or `postgres`: one fixed-window counter shared by every container (migration `004_rate_limits.sql`), fail-open if the database is unreachable. WebSocket per-message limits stay per connection
- **`DATABASE_MODE=pooler|direct`**: `direct` keeps asyncpg's prepared-statement cache on (one wire round trip per query instead of two) and lets `LISTEN` reuse `DATABASE_URL`. `EnvConfig` gained `database_mode`, `db_pool_size` and `database_connection_limit`, and refuses a `max_containers` that would exhaust the limit
- Vendor-neutral telemetry: the standard `OTEL_EXPORTER_OTLP_HEADERS` is honoured (Grafana's `GRAFANA_INSTANCE_ID` + `GRAFANA_OTLP_TOKEN` remain a shortcut). `APP_ENV` / `INSTANCE_ID` work on any host, with `MODAL_ENV` / `MODAL_TASK_ID` as fallbacks
- README "Swapping a layer": which port, adapter, variable and test to touch for each layer
- Containers are pinned to a configurable `region` (default `us-east`) next to the database

### Changed
- `tests/test_migrations.py` runner tests use a fixed three-migration baseline and derive the shipped list, so adding a migration no longer breaks them

### Added (schema migrations)
- Versioned migrations (`src/adapters/persistence/migrations.py`): `NNN_name.sql` applied once in numeric order and recorded in `schema_migrations` with a sha256; `R__name.sql` repeatable scripts re-run every start. The whole batch runs in one transaction under an advisory lock (same key as before, so old and new containers serialise during a rolling deploy): concurrent starts apply each migration exactly once, and a failing migration rolls the batch back. Editing an applied migration is refused (drift); a database newer than the code is tolerated with a warning so rollbacks work; existing databases are adopted without touching their data
- Previously the schema was re-run from `IF NOT EXISTS` scripts on every start, which could create tables but never change one
- `AUTO_MIGRATE=false` makes containers verify instead of migrate (they refuse to start on a database that is behind); `scripts/migrate.py status [--check]` / `up` run migrations as a deploy step; `scripts/check_supabase.py` reports migration status
- The security lockdown of `publish_event()` moved from `003` to the repeatable `R__publish_event_lockdown.sql`, so it is still re-asserted on every start
- `MIGRATION_LOCK_TIMEOUT_SECONDS` (120) and `MIGRATION_STATEMENT_TIMEOUT_SECONDS` (300)

### Changed (database round trips)
- Event publish is a single SQL function call (`publish_event()`, `sql/003_publish_event.sql`: lock + insert + NOTIFY) instead of a 5-statement transaction. The ordering lock is now held only while the database runs one statement, so the system-wide write ceiling no longer collapses with network distance: about 4,800 events/s next to the database (was 400–600) and 47–191/s at a 30 ms round trip (was ~6/s). The function is revoked from `PUBLIC`/`anon`/`authenticated`/`service_role` on every start, because Supabase would otherwise expose it through its REST API
- `list` is one statement (`count(*) OVER ()`), not a 4-statement transaction; `create`/`update` no longer pre-check names. **Adapters must now enforce name uniqueness and raise `DuplicateItemNameError` themselves** (the in-memory adapter does; the service no longer checks first). A service write is the write plus one publish
- Measured at a 30 ms round trip with 5 clients: list 20.8 → 46.2 req/s (median 238 → 105 ms), create 6.1 → 23.6 req/s (median 857 → 208 ms); a pool of 2 now matches what a pool of 5 did. `tests/test_round_trips.py` fails if a hot path costs more database calls than its budget

### Fixed / Added (webhook limit, smoke test)
- Webhook bodies are now bounded **while being read**: a chunked upload (no `Content-Length`) is cut off at `WEBHOOK_MAX_BODY_BYTES` instead of being buffered whole first (a 200 kB upload against a 1 kB limit used to read all 500 chunks; it now stops within 4)
- `scripts/smoke_test.py`: repeatable end-to-end check of a running deployment (auth, CRUD, uniqueness race, scopes, SSE, WebSocket, signed webhooks; cleans up after itself, `--yes` required for non-local hosts); `scripts/modal_mint_token.py` mints short-lived tokens inside Modal; verified 28/28 against Modal `feat`. See `docs/smoke-test.md`
- Docs corrected: event publish throughput is bounded by `1 / (5 × round-trip time)` under a database-wide lock (about 6 events/s at 30 ms, not a fixed 400–600/s), and the connections-per-container section now documents the 1–6 range and the measured cost of smaller pools

### Changed (prod and dev configuration, metrics)
- **Breaking for `prod`:** it now refuses to start with in-memory persistence or event bus unless `ALLOW_MEMORY_IN_PROD=true` (was a warning). Deploy it only after creating `supabase-db` in the Modal environment `prod`
- `dev` and `prod` presets mount `supabase-db` and `webhook-secrets` like `feat` (create them per environment first) and cap containers (`dev` 3, `prod` 5) to bound database connections
- Metrics carry a per-container `service.instance.id` (`MODAL_TASK_ID`): previously every container pushed the same series, so cumulative counters overwrote each other in Grafana
- `EnvConfig.metric_export_interval_seconds` (default 60, was 15): fewer requests per container. This does **not** remove Grafana Cloud's occasional `429 Too Many Requests` on OTLP metric batches: a 17-minute soak with 3 containers at 60 s still saw 8 retries and 2 failed batches (~1 export in 5 retried), and a probe with no app running saw some too, while a later probe saw none — it is intermittent and gateway-side. Counters and histograms are cumulative, so a failed batch loses one data point, not data. If it matters, check Grafana Cloud's usage/limits for the stack or ask Grafana support
- The OpenTelemetry startup log line reports each container's instance id and export interval

### Added (live verification on Modal `feat`)
- `EnvConfig.max_containers` (sent to Modal; validated; 3 on `feat`) and `webhook_secret_name` (mounts a Modal secret with `WEBHOOK_SECRET_<PROVIDER>`); `push_modal_secret.py` can push any keys with `--name`/`--keys` (secret-looking values are shown as `<hidden, N characters>`)
- `EnvConfig.stream_heartbeat_seconds` (default 5, exported as `STREAM_HEARTBEAT_SECONDS`): behind Modal's proxy the app learns of a client disconnect only when a heartbeat write fails, so a closed stream held its cap slot for ~10 s with the 15 s heartbeat and reconnecting clients got 503; with 5 s a freed slot is reusable in under a second
- Measured: moving Supabase from Seoul to `us-east-2` (next to Modal `us-east`) cut server-side request times 3.6–8.5× (docs/persistence.md)

### Added (Modal + Supabase)
- `scripts/push_modal_secret.py`: creates the `supabase-db` Modal secret from `.env` via the SDK (values never in argv or output), validates the connection strings first, refuses to overwrite without `--replace`, `--dry-run` shows key names only
- The `feat` preset mounts `supabase-db` (`persistence_secret_name`); `dev` and `prod` are unchanged. A real push requires an explicit `--environment` (secrets are per Modal environment and the default is often prod)

### Added (local Supabase testing)
- `.env` support for local runs: `.env.example`, `scripts/set_supabase_password.py` (hidden prompt, URL-encodes the password), `scripts/check_supabase.py` (read-only preflight that explains common mistakes and never prints the password), `scripts/run_local.py`
- **Security fix:** `.env` and its variants are now excluded from the Modal image (`modal_common.IMAGE_IGNORE`); previously `add_local_dir(".")` would have shipped a local `.env` into the image. A test checks this with Modal's own pattern matcher
- pytest refuses a non-local `TEST_DATABASE_URL` unless `ALLOW_DESTRUCTIVE_DB_TESTS=yes` (the Postgres tests drop tables), reads only `TEST_*` / `DB_POOL_*` from `.env`, and uses a separate `TEST_LISTEN_DATABASE_URL` for LISTEN tests
- The event bus verifies its LISTEN connection with a probe notification: a connection that succeeds but cannot receive (transaction pooler, wrong database) is detected, logged, and bypassed by polling instead of silently degrading

### Added (business metrics)
- `src/infrastructure/metrics.py` now defines the business instruments (`app.items.operations`, `app.items.operation.duration`, `app.events.published`, `app.events.publish.duration`, `app.events.delivery.lag`, `app.events.replayed`) with bounded label sets
- Recorded by telemetry sidecars (`MeteredItemService`, `MeteredEventBus` in `src/adapters/telemetry/metered.py`) wired in `deps.py` like `TracingProxy`; `core/` and the routes contain no telemetry, and a test enforces that

### Changed (metrics moved to the sidecar)
- Stream and webhook metrics are now recorded by `TelemetryMiddleware` from the wire instead of by calls inside the endpoints; `src/api/` contains no telemetry code (a test enforces it). The error handler stores the stable `error_code` on the request for the sidecar to read
- **Metric semantics changed:** SSE close reasons are now `client_disconnect` / `server_closed` / `error` (the old `max_duration` / `slow_consumer` split is gone for SSE — slow consumers are counted by the new `app.events.subscriptions.dropped`); WebSocket reasons come from close codes (new: `server_busy`, `rejected`, `other`); `stream.messages.sent` now counts every frame including heartbeats; `webhook.received` now also records `error` (publish failures)
- `provider` on `webhook.received` is only labelled for configured providers (unknown providers and wrong methods are not recorded) so URL probing cannot create unbounded series

### Fixed
- Postgres and in-memory event buses disconnected healthy subscribers when a burst exceeded their queue (a 64-slot queue received 47 of 2000 events); the pump now yields between chunks and the default queue is 256
- Docs claimed "thousands of events/s" for the Postgres bus; measured 400–600 events/s

### Added (shared streaming state)
- `EVENT_BUS_BACKEND=postgres`: events table + `LISTEN/NOTIFY` bus, Postgres webhook idempotency store and WebSocket ticket replay store, so SSE, WebSocket, webhook de-duplication and ticket single-use work across containers. Publishers serialise on an advisory lock so event ids are in commit order; works through a transaction pooler (`EVENT_BUS_LISTEN_URL` adds instant delivery over a session connection, polling otherwise)
- `TicketReplayStore` port with in-memory and Postgres adapters
- Topic scopes: `webhooks:read` / `items:read` are required in addition to `events:read` to subscribe to a topic, and WebSocket tickets carry only read scopes; `event_access.can_view()` hook for per-event visibility
- Rate limits: per user on `POST /ws/ticket`, per provider on webhooks, per connection on WebSocket messages (`429 RATE_LIMITED` / close `1008`)
- Contract tests run every EventBus / webhook store / ticket store backend through the same behaviour, plus cross-container tests (ordering under concurrent publishers, LISTEN loss and recovery, pruning)
- Route-label contract tests, a guard that the installed packages match the deploy pins, and `wait_until` in place of fixed sleeps in async tests

### Changed (shared streaming state)
- **Breaking:** subscribing to the `items` / `webhooks` streams now needs `items:read` / `webhooks:read` as well as `events:read`
- FastAPI's native request telemetry is disabled by default (`FASTAPI_NATIVE_TELEMETRY=true` restores it): it duplicated every server span and emitted a second, differently-unit `http.server.request.duration`
- `TelemetryMiddleware` resolves route labels through a function that can never fail a request
- `.github/requirements/test.txt` pins the same versions as the deploy image
- `statement_cache_size=0` is now verified against PgBouncer 1.20 and guarded by a test

### Added (persistence)
- Postgres persistence adapter (`PERSISTENCE_BACKEND=postgres`, `DATABASE_URL`) built for Supabase's transaction pooler: asyncpg pool opened in the app lifespan (never baked into a Modal snapshot), `statement_cache_size=0`, SSL required for remote hosts, idempotent schema bootstrap under an advisory lock (`src/adapters/persistence/sql/001_create_items.sql`)
- Name uniqueness enforced by a unique index on `lower(btrim(name))`, closing the check-then-write race across containers; violations surface as `DuplicateItemNameError` (409)
- `StorageUnavailableError` → `503 STORAGE_UNAVAILABLE` with `Retry-After` when the database is unreachable (health also reports unhealthy)
- `EnvConfig.persistence_secret_name` to mount a Modal secret holding `DATABASE_URL`
- Repository contract tests run against every backend; CI starts a Postgres service and sets `TEST_DATABASE_URL`

### Added (streaming)
- Server-Sent Events (`GET /events/stream`), WebSocket (`POST /ws/ticket`, `WS /ws`) and signed webhook ingestion (`POST /webhooks/{provider}`, generic HMAC-SHA256 with timestamp tolerance and idempotency) — see `docs/api/streaming.md`
- `EventBus` and `WebhookEventStore` ports with in-memory adapters; `ItemService` publishes `item.created` / `item.updated` / `item.deleted`
- `events:read` scope; WebSocket tickets are short-lived single-purpose JWTs with their own audience
- `EnvConfig.max_streams` (validated `< max_concurrent_requests`) and `stream_timeout_margin_seconds`; streams are capped per container and end themselves before the Modal timeout
- Stream and webhook metrics (`stream.connections.active/closed`, `stream.messages.sent`, `webhook.received`) and one span per WebSocket connection

### Changed (streaming)
- `TelemetryMiddleware` is now a pure ASGI middleware (was `BaseHTTPMiddleware`) so streaming responses are unbuffered and WebSockets are supported; the in-flight gauge is labelled by method only
- Metric `route` labels resolve through `scope["route"]`, fixing raw-URL labels on included routers (unbounded cardinality) and collapsing unmatched paths to `unmatched`

### Changed
- **Breaking:** all error responses now share one flat body — `{"detail": str, "session_id": str, "error_code": str}` plus an `errors` list for validation failures. Auth errors previously nested this under `detail`, and 404s were a bare string
- **Breaking:** `GET /items` and `GET /items/{id}` now require the `items:read` scope (previously any valid token)
- **Breaking:** `get_current_user` / `require_scope` now return a `TokenPayload` model instead of a dict
- Response-body `session_id` now equals the `x-session-id` header (generated once in `TelemetryMiddleware`, read from `request.state.session_id`)
- JWT verification requires `exp` and `sub`, optionally enforces `JWT_AUDIENCE` / `JWT_ISSUER`, and validates claims into `TokenPayload` (now includes `scopes`)
- `dev` no longer mounts the `grafana-otlp` secret; telemetry is gated by `EnvConfig.telemetry_enabled` and `TELEMETRY_ENABLED`
- `GET /items` validates `limit` (1–500) and `offset` (>= 0)
- Startup now validates `JWT_SECRET` (min 32 chars) and the persistence backend, so bad config stops the container instead of failing the first request
- Item names must be unique (case/whitespace-insensitive); duplicates return `409 ITEM_NAME_EXISTS`
- `EnvConfig` in `modal_common.py` now centralizes app and server metadata (`app_version`, `app_description`, `server_host`, `server_port`, `server_prefix`) and adds `max_concurrent_requests`
- Deployment wiring in `modal_app.py` now uses `@modal.concurrent(max_inputs=env_config.max_concurrent_requests)` and local `uvicorn` startup reads host/port from environment config
- FastAPI app metadata and API prefix in `src/main.py` now come from environment config instead of hardcoded values
- Health response metadata in `src/api/routes.py` now reports environment-aware `service_name` and config-driven `version`
- Modal environment domain field renamed from `custom_domain` to `server_domain` across environment definitions

### Added
- `src/api/errors.py`: `AppError`, `get_session_id` and `register_exception_handlers` (handles `AppError`, `HTTPException`, `RequestValidationError`, `DuplicateItemNameError` and unexpected exceptions)
- `FieldError` model and `ErrorDetail.errors`; new codes `ITEM_NOT_FOUND`, `NOT_FOUND`, `METHOD_NOT_ALLOWED`, `INTERNAL_ERROR`
- Startup warning when `PERSISTENCE_BACKEND=memory` and `MODAL_ENV=prod`
- Dedicated CI dependency files: `.github/requirements/test.txt` and `.github/requirements/docs.txt`
- Lightweight `modal` module stub in `tests/conftest.py` so tests can run in local/CI contexts where `modal` is not installed

### Fixed
- `scripts/jwt_token_generator.py` now honours `--scopes` (comma-separated)
- GitHub workflows now install dependencies from repository-scoped requirement files (`.github/workflows/app-testing.yml`, `.github/workflows/docs.yml`)
- Health endpoint test expectation updated to validate the environment-aware service name format

---

## [1.0.0] — 2026-05-03

Initial production release of `modal-template-fastapi`.

### Added

#### Application
- FastAPI application factory (`src/main.py`) with lifespan context manager for startup/shutdown logging
- `CORSMiddleware` configured to allow all origins, methods, and headers
- Global `RequestValidationError` handler returning structured `ErrorDetail` with `VALIDATION_ERROR` error code and UUID `session_id`
- API router mounted under `/api/v1` prefix

#### API Endpoints
- `GET /api/v1/health` — public health check returning `HealthCheckResponse` with `status`, `service_name`, `version`, and `services_summary`
- `GET /api/v1/items` — authenticated endpoint returning all stored items
- `POST /api/v1/items` — authenticated endpoint creating a new item, returns `201 Created`
- `GET /api/v1/items/{item_id}` — authenticated endpoint retrieving a single item by UUID, returns `404` if not found
- `PUT /api/v1/items/{item_id}` — authenticated endpoint replacing item data in full, returns `404` if not found
- `DELETE /api/v1/items/{item_id}` — authenticated endpoint removing an item, returns `204 No Content`

#### Authentication
- JWT Bearer authentication via `PyJWT` (`src/api/auth.py`)
- FastAPI `HTTPBearer` dependency (`get_current_user`) injected into all protected routes
- `verify_token()` function with distinct error handling for `ExpiredSignatureError` and `InvalidTokenError`
- Machine-readable error codes: `MISSING_TOKEN`, `TOKEN_EXPIRED`, `INVALID_TOKEN`
- `JWT_SECRET` and `JWT_ALGORITHM` read from environment at call time for runtime secret injection
- `WWW-Authenticate: Bearer` response header on all `401` responses per RFC 6750

#### Pydantic Models (`src/api/models.py`)
- `BaseResponse` — base class carrying `session_id` (UUID v4) on every response
- `GenericRequest` — free-form `data` dict payload with optional `project_id` (min length 1)
- `ItemResponse` — extends `BaseResponse` with `status`, `message`, and optional `data`
- `HealthCheckResponse` — extends `BaseResponse` with `HealthStatus` enum, `service_name`, `version`, `services_summary`
- `ErrorDetail` — structured error envelope with `detail`, optional `session_id`, optional `error_code`
- `TokenPayload` — decoded JWT model with `sub`, `exp`, and optional `iat`
- `FileUploadRequest` / `FileUploadResponse` — scaffold models for file upload operations

#### Business Logic
- In-memory item store (`handler.py`) backed by a module-level `Dict[str, Dict[str, Any]]`
- UUID v4 auto-generated `id` on item creation
- Full CRUD: `create_item`, `get_item`, `list_items`, `update_item`, `delete_item`

#### Modal Deployment (`modal_app.py`, `modal_common.py`)
- Debian Slim container image with Python 3.10, APT packages (`curl`, `jq`), and pip packages installed via `uv`
- `EnvConfig` dataclass with fields for `env_name`, `app_name`, `custom_domain`, `cpu_core_count`, `ram_memory_mib`, `gpu_type`, `server_hard_timeout_seconds`, `min_containers`, `secrets`, and `volumes`
- Three named environments: `feat` (scale-to-zero), `dev` (scale-to-zero), `prod` (`min_containers=1`)
- `get_env_config()` registry validator — raises `ValueError` on unknown environment names
- `build_fastapi_config()` — translates `EnvConfig` to Modal function kwargs dict
- Modal Volume (`fastapi-volume`) mounted at `/root/fastapi-volume`, created if missing
- `@modal.asgi_app()` ASGI bridge connecting Modal to the FastAPI app
- `@app.local_entrypoint()` for local `uvicorn` development without cloud deployment

#### Testing (`tests/`)
- `conftest.py` with `JWT_SECRET` set via `os.environ.setdefault()` before all imports
- `autouse` `clear_store` fixture wiping `handler._store` before and after every test
- `client` fixture with `get_current_user` dependency overridden for business-logic tests
- `auth_client` fixture with real JWT verification for auth-specific tests
- `make_token()` factory for generating JWTs with arbitrary `sub`, `exp_offset`, `secret`, and `algorithm`
- Fixtures: `valid_token`, `expired_token`, `wrong_secret_token`, `auth_headers`
- `test_health.py` — 8 tests covering status, schema, uniqueness, and public access
- `test_auth.py` — 12 tests covering missing, malformed, expired, wrong-secret, and valid token paths
- `test_items.py` — 30 tests covering full CRUD lifecycle, validation, isolation, and edge cases
- `test_models.py` — 29 tests covering all Pydantic models, required fields, optional fields, and constraints
- `pytest.ini` with `testpaths = tests` and `pythonpath = .`
- `.github/requirements/test.txt` with `pytest`, `httpx`, `pytest-asyncio`, `PyJWT`, `fastapi`, `pydantic`

#### CI/CD (`.github/workflows/`)
- `app-testing.yml` — reusable pytest workflow triggered on `pull_request` to `dev`/`production` and via `workflow_call`
- `modal-deploy.yml` — deploy pipeline with `test` job (calls `app-testing.yml`) and `deploy` job gated by `needs: test`
- Environment routing: `push feat/**` → `feat`, merged PR → `dev` → `dev`, merged PR → `production` → `prod`
- `Validate deployment config` step that fails fast if no environment variable is resolved
- All Actions upgraded to Node.js 24 runtime (`actions/checkout@v6`, `actions/setup-python@v6`)

#### Documentation
- MkDocs Material site with indigo theme, dark/light mode toggle, Inter + JetBrains Mono fonts
- Navigation tabs, instant loading, search with highlight, per-page ToC, and content copy buttons
- `docs/index.md` — hero landing page with gradient title, shield.io badges, feature card grid, quick start, project layout
- `docs/architecture.md` — component diagram, request lifecycle sequence diagram, auth flowchart, class diagram, module reference, environment matrix
- `docs/api/index.md` — API section landing with base URLs and endpoint summary
- `docs/api/endpoints.md` — live Swagger UI embed, all 5 endpoints with tabbed curl examples, sequence diagrams, schema tables, error reference
- `docs/api/auth.md` — JWT structure, verification pipeline flowchart, dependency injection pattern, error table, token generation snippets
- `docs/testing.md` — suite architecture, fixture reference, per-module checklist, isolation strategy
- `docs/cicd.md` — pipeline flowchart, workflow reference, trigger matrix, secrets/variables table, branching gitGraph
- `docs/deployment.md` — principles card grid, environment matrix, `EnvConfig` reference, container image breakdown, operational runbook, new-environment guide
- `docs/stylesheets/extra.css` — custom CSS with hero gradient, card hover animations, HTTP method badges (colour-coded, dark-mode aware), table and scrollbar polish
- `docs.yml` GitHub Action building and publishing to `gh-pages` on changes to `docs/`, `mkdocs.yml`, `swagger.yaml`, or `src/`
- `swagger.yaml` OpenAPI 3.0 specification covering all endpoints, schemas, security scheme, and error responses

---

## [0.1.0] — 2026-05-03

### Added
- Initial project scaffold: `src/`, `modal_app.py`, `modal_common.py`
- Placeholder `README.md`
- `.gitignore` for Python projects

---

[Unreleased]: https://github.com/your-org/modal-template-fastapi/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/your-org/modal-template-fastapi/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/your-org/modal-template-fastapi/releases/tag/v0.1.0
