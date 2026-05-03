# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Types of changes**
> `Added` · `Changed` · `Deprecated` · `Removed` · `Fixed` · `Security`

---

## [Unreleased]

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
- `requirements-test.txt` with `pytest`, `httpx`, `pytest-asyncio`, `PyJWT`, `fastapi`, `pydantic`

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
