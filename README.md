# modal-template-fastapi

> A production-ready FastAPI template deployed serverlessly on Modal — JWT authentication, full CRUD API, OpenTelemetry observability with Grafana Cloud, 79-test pytest suite, and automated CI/CD.

[![Python](https://img.shields.io/badge/Python-3.11-4f46e5?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-latest-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Modal](https://img.shields.io/badge/Modal-Serverless-7c3aed?style=flat-square)](https://modal.com)
[![Tests](https://img.shields.io/badge/Tests-79%20passing-15803d?style=flat-square&logo=pytest&logoColor=white)](tests/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-enabled-f5a800?style=flat-square&logo=opentelemetry&logoColor=white)](src/observability/)
[![Grafana](https://img.shields.io/badge/Grafana-Cloud-f46800?style=flat-square&logo=grafana&logoColor=white)](docs/OBSERVABILITY.md)
[![Docs](https://img.shields.io/badge/Docs-GitHub%20Pages-4f46e5?style=flat-square&logo=materialformkdocs&logoColor=white)](https://your-org.github.io/modal-template-fastapi/)
[![License](https://img.shields.io/badge/License-MIT-6b7280?style=flat-square)](LICENSE)

**[📖 Full Documentation →](https://your-org.github.io/modal-template-fastapi/)**

---

## What is this?

`modal-template-fastapi` is a battle-tested starting point for shipping Python APIs to the cloud. It combines the ergonomics of FastAPI with Modal's serverless platform — giving you autoscaling, secret management, persistent volumes, and environment-aware deployments without managing any infrastructure.

Clone it, swap in your business logic, and ship in minutes.

**Key features:**

- 🔒 JWT Bearer authentication with typed error codes (`MISSING_TOKEN`, `TOKEN_EXPIRED`, `INVALID_TOKEN`)
- 🗄️ Full CRUD REST API with Pydantic v2 validation and `session_id` tracing on every response
- ☁️ Modal serverless deployment with `feat` / `dev` / `prod` environments via a declarative `EnvConfig` dataclass
- 📡 OpenTelemetry observability — distributed traces, 5 HTTP metrics, log-trace correlation, cold-start tracking
- 📊 Grafana Cloud integration — OTLP/HTTP push, zero idle cost, enabled per environment via Modal Secret
- ✅ 79-test pytest suite — isolated, no external services, runs in < 1 second
- 🚀 GitHub Actions CI/CD — tests gate every deploy (`needs: test`)
- 📖 MkDocs Material documentation hosted on GitHub Pages

---

## Project Structure

```
modal-template-fastapi/
├── src/
│   ├── main.py              # FastAPI app, CORS, exception handlers
│   ├── api/
│   │   ├── auth.py          # JWT verification + FastAPI dependency
│   │   ├── handler.py       # CRUD logic (swap for your DB here)
│   │   ├── models.py        # Pydantic v2 request/response models
│   │   └── routes.py        # All 5 API endpoints
│   └── observability/       # OpenTelemetry layer (template-owned)
│       ├── __init__.py      # Public re-export surface
│       ├── setup.py         # SDK bootstrap + Grafana Cloud OTLP exporter
│       ├── middleware.py    # Auto-instrumentation for every HTTP request
│       └── metrics.py       # Stub — replace with domain instruments per service
├── tests/                   # pytest suite (79 tests)
├── modal_app.py             # Modal Cls + @enter telemetry init + ASGI wrapper
├── modal_common.py          # EnvConfig, container image, environment registry
├── docs/OBSERVABILITY.md    # Grafana Cloud setup guide + PromQL queries
├── swagger.yaml             # OpenAPI 3.0 spec
└── .github/workflows/
    ├── app-testing.yml      # Reusable pytest CI
    ├── modal-deploy.yml     # Test → deploy pipeline
    └── docs.yml             # Documentation → GitHub Pages
```

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

# 4. Start the API locally (no cloud deployment needed)
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

## Observability

The template ships a complete OpenTelemetry layer that works out of the box with [Grafana Cloud](https://grafana.com/products/cloud/) (free tier).

**What's recorded automatically on every request — zero per-route code:**

| Signal | Detail |
|---|---|
| Distributed trace | Span per request with method, route template, status, client IP, user agent, session ID |
| Request rate | `http.server.request.count` counter |
| Latency p50/p95/p99 | `http.server.request.duration` histogram (ms) |
| Error rate | `http.server.error.count` counter (4xx + 5xx) |
| In-flight requests | `http.server.active_requests` up-down counter |
| Cold starts | `app.container.cold_start.count` — Modal-specific, spikes = containers scaling up |
| Log correlation | Every log line carries `otelTraceID` + `otelSpanID` for Grafana Loki linking |
| W3C trace propagation | Incoming `traceparent` headers are respected — spans become children of upstream traces |

**Environment strategy:** `feat` and `prod` push telemetry; `dev` is silent (zero cost). Controlled by the `grafana-otlp` Modal Secret — no code changes needed to enable or disable per environment.

**One-time Grafana Cloud setup:**

```bash
modal secret create grafana-otlp \
  GRAFANA_INSTANCE_ID=<numeric stack ID> \
  GRAFANA_OTLP_TOKEN=<API token with metrics:write traces:write> \
  GRAFANA_OTLP_ENDPOINT=<https://otlp-gateway-<region>.grafana.net/otlp>
```

> Full setup guide, metrics reference table, PromQL queries, and copy-to-new-service checklist: [OBSERVABILITY.md →](docs/OBSERVABILITY.md)

---

## API at a Glance

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/health` | None | Service health check |
| `GET` | `/api/v1/items` | JWT | List all items |
| `POST` | `/api/v1/items` | JWT | Create an item |
| `GET` | `/api/v1/items/{id}` | JWT | Get a single item |
| `PUT` | `/api/v1/items/{id}` | JWT | Replace an item |
| `DELETE` | `/api/v1/items/{id}` | JWT | Delete an item |

> Full endpoint reference with curl examples, sequence diagrams, and a live Swagger UI: [API docs →](https://your-org.github.io/modal-template-fastapi/api/endpoints/)

---

## Documentation

The full documentation is hosted on GitHub Pages and covers:

| Section | Contents |
|---|---|
| [Architecture](https://your-org.github.io/modal-template-fastapi/architecture/) | Component diagrams, request lifecycle, module reference |
| [API Reference](https://your-org.github.io/modal-template-fastapi/api/endpoints/) | Endpoints, curl examples, sequence diagrams, Swagger UI |
| [Authentication](https://your-org.github.io/modal-template-fastapi/api/auth/) | JWT flow, error codes, token generation |
| [Testing](https://your-org.github.io/modal-template-fastapi/testing/) | Fixture reference, test matrix, isolation strategy |
| [CI/CD](https://your-org.github.io/modal-template-fastapi/cicd/) | Workflow diagrams, trigger matrix, required secrets |
| [Deployment](https://your-org.github.io/modal-template-fastapi/deployment/) | EnvConfig reference, environment matrix, operational runbook |
| [Observability](docs/OBSERVABILITY.md) | Grafana Cloud setup, metrics reference, PromQL queries, copy-to-new-service checklist |

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
