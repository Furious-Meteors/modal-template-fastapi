# modal-template-fastapi

> A production-ready FastAPI template deployed serverlessly on Modal — Hexagonal Architecture, JWT authentication with scope-based authorisation, paginated CRUD API, telemetry sidecar pattern, OpenTelemetry observability with Grafana Cloud, 145-test pytest suite, and automated CI/CD.

[![Python](https://img.shields.io/badge/Python-3.11-4f46e5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-latest-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Modal](https://img.shields.io/badge/Modal-Serverless-7c3aed?style=flat-square)](https://modal.com)
[![Tests](https://img.shields.io/badge/Tests-145%20passing-15803d?style=flat-square&logo=pytest&logoColor=white)](tests/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-enabled-f5a800?style=flat-square&logo=opentelemetry&logoColor=white)](src/infrastructure/)
[![Grafana](https://img.shields.io/badge/Grafana-Cloud-f46800?style=flat-square&logo=grafana&logoColor=white)](docs/OBSERVABILITY.md)
[![Docs](https://img.shields.io/badge/Docs-GitHub%20Pages-4f46e5?style=flat-square&logo=materialformkdocs&logoColor=white)](https://your-org.github.io/modal-template-fastapi/)
[![License](https://img.shields.io/badge/License-MIT-6b7280?style=flat-square)](LICENSE)

**[📖 Full Documentation →](https://your-org.github.io/modal-template-fastapi/)**

---

## What is this?

`modal-template-fastapi` is a battle-tested starting point for shipping Python microservices to the cloud. It combines the ergonomics of FastAPI with Modal's serverless platform — giving you autoscaling, secret management, persistent volumes, and environment-aware deployments without managing any infrastructure.

The template is built on **Hexagonal Architecture (Ports & Adapters)** so every layer — persistence, telemetry, HTTP — can be swapped independently without touching business logic. Clone it, drop in your domain, wire your adapter, and ship.

**Key features:**

- 🏛️ **Hexagonal Architecture** — clean `core/ports/services`, concrete `adapters/`, single wiring point in `deps.py`
- 🔒 **JWT auth + scope-based authorisation** — `require_scope("items:write")` dependency, typed error codes
- 📄 **Paginated CRUD API** — typed `ItemRequest`, `PaginatedItemsResponse` with `total`, `has_more`, `limit`, `offset`
- 🔭 **Telemetry sidecar** — `TracingProxy` wraps any adapter generically; domain and adapters have zero OTel knowledge
- 📡 **OpenTelemetry** — distributed traces, 5 HTTP metrics, log-trace correlation, cold-start tracking
- 📊 **Grafana Cloud** — OTLP/HTTP push, zero idle cost, enabled per environment via Modal Secret
- ☁️ **Modal serverless** — `feat` / `dev` / `prod` environments via `EnvConfig`, memory snapshots for fast cold starts
- 🌐 **CORS per-environment** — `cors_origins` field per `EnvConfig`, propagated through env vars
- 🔌 **Platform-decoupled** — `src/` has zero Modal imports; runs on any ASGI host unchanged
- ✅ **145-test pytest suite** — isolated per-test repositories, no stubs, runs in < 0.3 seconds
- 🚀 **GitHub Actions CI/CD** — branch-based environment routing, tests gate every deploy

---

## Project Structure

```
modal-template-fastapi/
├── src/
│   ├── main.py                        # FastAPI app, CORS, exception handlers
│   ├── deps.py                        # Wiring: adapter → service, startup validation
│   ├── core/                          # Pure business logic — zero I/O imports
│   │   ├── domain/
│   │   │   └── item.py               # Item entity (replace with your domain)
│   │   ├── ports/
│   │   │   └── item_repository.py    # ItemRepository Protocol (the contract)
│   │   └── services/
│   │       └── item_service.py       # Business logic, depends only on ports
│   ├── adapters/                      # Concrete implementations — know about core, not HTTP
│   │   ├── http/
│   │   │   └── middleware.py         # TelemetryMiddleware — inbound HTTP sidecar
│   │   ├── persistence/
│   │   │   └── memory.py             # InMemoryItemRepository (replace with Postgres, Dynamo, etc.)
│   │   └── telemetry/
│   │       └── tracing_proxy.py      # TracingProxy — generic outbound port sidecar
│   ├── api/                           # HTTP layer — routes, schemas, auth
│   │   ├── auth.py                   # JWT verification + get_current_user + require_scope
│   │   ├── models.py                 # Pydantic v2 request/response schemas
│   │   └── routes.py                 # API endpoints — delegates to ItemService
│   ├── infrastructure/                # Cross-cutting setup — runs once at startup
│   │   ├── setup.py                  # OTel SDK bootstrap, OTLP exporters, Grafana auth
│   │   └── metrics.py                # Domain metric instruments stub (replace per service)
│   └── utils/
│       └── config.py                 # App constants read from env vars
├── tests/                             # pytest suite (145 tests)
│   ├── conftest.py                   # Fixtures: fresh repo per test, JWT helpers, scope tokens
│   ├── test_auth.py                  # JWT authentication tests
│   ├── test_auth_scopes.py           # Scope-based authorisation tests
│   ├── test_health.py                # Health endpoint tests
│   ├── test_http_middleware.py       # TelemetryMiddleware behaviour tests
│   ├── test_infrastructure_setup.py  # OTel SDK bootstrap tests
│   ├── test_items.py                 # CRUD + pagination tests
│   └── test_models.py                # Pydantic model validation tests
├── modal_app.py                       # Modal Cls, @enter telemetry init, ASGI wrapper
├── modal_common.py                    # EnvConfig, container image, environment registry
├── scripts/
│   └── jwt_token_generator.py        # CLI tool: generate JWT tokens for testing
├── docs/OBSERVABILITY.md             # Grafana Cloud setup + PromQL queries
├── swagger.yaml                       # OpenAPI 3.0 spec
└── .github/workflows/
    ├── app-testing.yml                # Reusable pytest CI
    ├── modal-deploy.yml               # Test → deploy pipeline
    └── docs.yml                       # Documentation → GitHub Pages
```

---

## Architecture

The template is built on **Hexagonal Architecture**. The dependency rule is strict — everything points inward toward `core/`. Adapters know about `core/`, but `core/` never knows about adapters.

```
                   ┌─────────────────────────────────────┐
                   │          src/infrastructure/        │
                   │    OTel SDK setup — runs once       │
                   └────────────────┬────────────────────┘
                                    │ get_tracer() / get_meter()
          ┌─────────────────────────▼─────────────────────────────────┐
          │                    src/adapters/                          │
          │                                                           │
          │  http/middleware.py   ← TelemetryMiddleware (HTTP sidecar)│
          │  telemetry/tracing_proxy.py ← TracingProxy (port sidecar) │
          │  persistence/memory.py  ← InMemoryItemRepository          │
          └──────────────────────┬────────────────────────────────────┘
                                 │ ItemRepository (port)
          ┌──────────────────────▼─────────────────────────────────────┐
          │                     src/core/                              │
          │                                                            │
          │  domain/item.py       ← pure Item entity                   │
          │  ports/item_repository.py  ← abstract Protocol             │
          │  services/item_service.py  ← business logic                │
          └────────────────────────────────────────────────────────────┘
```

**Swapping persistence** (e.g. Postgres) requires 3 steps:
1. Add `src/adapters/persistence/postgres.py`
2. Add one `elif` branch in `src/deps.py`
3. Set `PERSISTENCE_BACKEND=postgres` + `DATABASE_URL=...` in your Modal secret

No routes, services, domain, or telemetry files change.

---

## Local Development

```bash
# 1. Clone and install test dependencies
git clone https://github.com/your-org/modal-template-fastapi.git
cd modal-template-fastapi
pip install -r .github/requirements/test.txt

# 2. Run the test suite
pytest tests/ -v

# 3. Authenticate with Modal (first time only)
modal setup

# 4. Start the API locally
MODAL_ENV=dev modal run modal_app.py
# → http://localhost:8000/api/v1/health
```

---

## Deployment

```bash
# Deploy to a named environment manually
MODAL_ENV=feat modal deploy modal_app.py

# Or push a branch — CI/CD handles the rest
git push origin feat/my-feature
```

**CI/CD pipeline:** push to `feat/**` or merge a PR to `dev`/`production` triggers GitHub Actions, which runs the full test suite first. If tests pass, `modal deploy` fires automatically to the correct environment. If tests fail, the deploy is blocked.

> See the [CI/CD guide](https://your-org.github.io/modal-template-fastapi/cicd/) and [Deployment guide](https://your-org.github.io/modal-template-fastapi/deployment/) for full details.

---

## API Reference

| Method | Endpoint | Auth | Scope | Description |
|---|---|---|---|---|
| `GET` | `/api/v1/health` | None | — | Service health check |
| `GET` | `/api/v1/items` | JWT | — | List items (paginated) |
| `GET` | `/api/v1/items/{id}` | JWT | — | Get a single item |
| `POST` | `/api/v1/items` | JWT | `items:write` | Create an item |
| `PUT` | `/api/v1/items/{id}` | JWT | `items:write` | Update an item |
| `DELETE` | `/api/v1/items/{id}` | JWT | `items:write` | Delete an item |

**Paginated list response:**
```json
{
  "items": [...],
  "total": 42,
  "limit": 10,
  "offset": 0,
  "has_more": true
}
```

**JWT scopes:** include `"scopes": ["items:read", "items:write"]` in your JWT payload. Read endpoints accept any valid token; write endpoints require `items:write`.

Generate a token for testing:
```bash
python scripts/jwt_token_generator.py --secret <JWT_SECRET>
```

> Full endpoint reference with curl examples and Swagger UI: [API docs →](https://your-org.github.io/modal-template-fastapi/api/endpoints/)

---

## Authentication & Authorisation

The template uses **JWT Bearer tokens** with scope-based authorisation.

```python
# Any valid token — read access
current_user: dict = Depends(get_current_user)

# Requires "items:write" scope in JWT payload
current_user: dict = Depends(require_scope("items:write"))
```

**JWT payload shape:**
```json
{
  "sub": "user@example.com",
  "exp": 1234567890,
  "scopes": ["items:read", "items:write"]
}
```

**Error codes:** `MISSING_TOKEN` (401), `TOKEN_EXPIRED` (401), `INVALID_TOKEN` (401), `INSUFFICIENT_SCOPE` (403).

**Modal secret setup:**
```bash
modal secret create fastapi-auth-secrets \
  JWT_SECRET=<your-secret-at-least-32-chars> \
  JWT_ALGORITHM=HS256
```

---

## Observability

Telemetry is a **sidecar** — domain and adapters have zero OTel knowledge. Two sidecars cover all signals automatically:

| Sidecar | Where | What it adds |
|---|---|---|
| `TelemetryMiddleware` | HTTP boundary | Request span, duration, status, session ID |
| `TracingProxy` | Port boundary | Child span per repository method call |

**Signals recorded automatically — zero per-route code:**

| Signal | Detail |
|---|---|
| Distributed trace | Span per request — method, route template, status, client IP, session ID |
| Request rate | `http.server.request.count` counter |
| Latency p50/p95/p99 | `http.server.request.duration` histogram (ms) |
| Error rate | `http.server.error.count` counter (4xx + 5xx) |
| In-flight requests | `http.server.active_requests` up-down counter |
| Cold starts | `app.container.cold_start.count` — spikes = containers scaling up |
| Log correlation | Every log line carries `otelTraceID` + `otelSpanID` |
| W3C trace propagation | Incoming `traceparent` headers respected — spans become children of upstream traces |

**Environment strategy:** `feat` and `prod` push telemetry; `dev` is silent (zero cost). Controlled by the `grafana-otlp` Modal Secret — no code changes needed.

**One-time Grafana Cloud setup:**
```bash
modal secret create grafana-otlp \
  GRAFANA_INSTANCE_ID=<numeric stack ID> \
  GRAFANA_OTLP_TOKEN=<API token with metrics:write traces:write> \
  GRAFANA_OTLP_ENDPOINT=<https://otlp-gateway-<region>.grafana.net/otlp>
```

> Full setup guide, PromQL queries, and copy-to-new-service checklist: [OBSERVABILITY.md →](docs/OBSERVABILITY.md)

---

## Adapting This Template

### Replace the domain

1. Edit `src/core/domain/item.py` — add your typed fields, update `to_dict()`
2. Edit `src/core/services/item_service.py` — adjust business logic
3. Edit `src/api/models.py` — update `ItemRequest` with your request schema
4. Edit `src/api/routes.py` — update route handlers

### Add a persistence backend

1. Create `src/adapters/persistence/<backend>.py` — implement `get`, `list`, `create`, `update`, `delete`
2. Add the backend to `_REQUIRED_ENV` in `src/deps.py` (with its required env vars)
3. Add an `elif backend == "<backend>"` branch in `_get_repository()`
4. Set `PERSISTENCE_BACKEND=<backend>` in your Modal secret

### Add a new service

1. Add domain entity + port + service under `src/core/`
2. Add adapter under `src/adapters/`
3. Add a `get_<service>()` dependency in `src/deps.py`
4. Wire into routes via `Depends(get_<service>())`

`TracingProxy` covers telemetry for any new service automatically — no new observability files needed.

---

## CORS Configuration

CORS origins are configured per environment in `modal_common.py`:

```python
FEAT = EnvConfig(cors_origins=["*"])   # replace with your Modal domain
PROD = EnvConfig(cors_origins=["*"])   # replace with your Modal domain
```

Replace `["*"]` with your actual Modal app URL (e.g. `["https://<username>--<app>-prod.modal.run"]`) when your domains are known. The value propagates through `configure_env_vars` → `CORS_ORIGINS` env var → `src/utils/config.py` — no other files need touching.

---

## Documentation

| Section | Contents |
|---|---|
| [Architecture](https://your-org.github.io/modal-template-fastapi/architecture/) | Component diagrams, request lifecycle, module reference |
| [API Reference](https://your-org.github.io/modal-template-fastapi/api/endpoints/) | Endpoints, curl examples, pagination, Swagger UI |
| [Authentication](https://your-org.github.io/modal-template-fastapi/api/auth/) | JWT flow, scopes, error codes, token generation |
| [Testing](https://your-org.github.io/modal-template-fastapi/testing/) | Fixture reference, test matrix, isolation strategy |
| [CI/CD](https://your-org.github.io/modal-template-fastapi/cicd/) | Workflow diagrams, trigger matrix, required secrets |
| [Deployment](https://your-org.github.io/modal-template-fastapi/deployment/) | EnvConfig reference, environment matrix, operational runbook |
| [Observability](docs/OBSERVABILITY.md) | Grafana Cloud setup, metrics reference, PromQL queries |

---

## Contributing

Contributions are welcome. Please follow these steps:

1. Fork the repository and create a branch from `dev`
2. Make your changes — add tests for any new behaviour
3. Run `pytest tests/ -v` and confirm all tests pass
4. Open a pull request targeting `dev` with a clear description of the change

For significant changes, open an issue first to discuss the approach.

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.
