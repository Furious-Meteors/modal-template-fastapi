# Architecture

A bird's-eye view of how the project is structured, how components relate, and how a request moves from a client to a response.

---

## System Overview

The project is composed of three distinct layers that collaborate to deliver a production-grade serverless API:

| Layer | Components | Responsibility |
|---|---|---|
| **Infrastructure** | Modal, GitHub Actions | Compute scheduling, secret injection, CI/CD |
| **Application** | FastAPI, Pydantic, PyJWT | HTTP handling, validation, authentication |
| **Data** | In-memory `_store` dict | Ephemeral item storage (swap for a real DB) |

---

## High-Level Component Diagram

```mermaid
graph TD
    Dev["👤 Developer"] -->|git push / PR merge| GH["GitHub Repository"]

    GH -->|triggers| GHA["GitHub Actions"]
    GHA -->|test job| PT["pytest — 79 tests"]
    PT -->|✅ pass| GHA
    PT -->|❌ fail| BLOCK["🚫 Deploy Blocked"]

    GHA -->|deploy job| MD["modal deploy modal_app.py"]
    MD -->|provisions| MC["Modal Container\nPython 3.11 · Debian Slim"]

    MC -->|serves| ASGI["@modal.asgi_app()\nASGI bridge"]
    ASGI -->|mounts| FA["FastAPI App\nsrc/main.py"]

    FA -->|CORS · exception handlers| MW["Middleware Layer"]
    MW -->|dispatches| RT["Router\n/api/v1/*"]

    RT -->|public| HC["GET /health"]
    RT -->|JWT guard| AUTH["get_current_user()"]
    AUTH -->|authorized| CRUD["CRUD Handler\nhandler.py"]
    AUTH -->|rejected| ERR["401 ErrorDetail"]

    CRUD --> STORE[("In-Memory Store\n_store: Dict")]

    style BLOCK fill:#fee2e2,stroke:#b91c1c,color:#b91c1c
    style ERR fill:#fee2e2,stroke:#b91c1c,color:#b91c1c
    style STORE fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    style MC fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a
    style FA fill:#dcfce7,stroke:#15803d,color:#14532d
```

---

## Request Lifecycle

How a single authenticated API call flows end-to-end through the stack.

```mermaid
sequenceDiagram
    autonumber
    participant C  as Client
    participant M  as Modal ASGI Bridge
    participant MW as FastAPI Middleware
    participant D  as get_current_user()
    participant R  as Router / Route Handler
    participant H  as handler.py
    participant S  as _store

    C->>M: HTTPS request + Bearer token
    M->>MW: forward via ASGI protocol

    Note over MW: CORS headers applied

    MW->>D: inject HTTPAuthorizationCredentials
    D->>D: jwt.decode(token, JWT_SECRET)

    alt Token invalid or missing
        D-->>C: 401 Unauthorized + ErrorDetail
    end

    D-->>R: decoded payload {sub, exp, iat}
    R->>H: handler.create_item(request)
    H->>S: _store[uuid] = {...}
    S-->>H: stored item dict
    H-->>R: item dict
    R-->>MW: ItemResponse(session_id, status, data)
    MW-->>M: JSON response
    M-->>C: 201 Created
```

---

## Module Reference

### `modal_app.py` — Entry Point

The thin bridge between Modal and FastAPI.

```python
@app.function(**build_fastapi_config(env_config))  # (1)
@modal.asgi_app()                                   # (2)
def fastapi_app():
    from src.main import app as fastapi_app
    return fastapi_app                              # (3)
```

1. Applies all hardware, secret, and volume config from `EnvConfig`
2. Tells Modal this function handles ASGI traffic (HTTP/WebSocket)
3. Returns the FastAPI app instance — Modal routes all requests into it

The `@app.local_entrypoint()` allows `modal run modal_app.py` to start a local `uvicorn` server without deploying.

---

### `modal_common.py` — Environment Configuration

Central configuration registry. Two primary responsibilities:

**1. Container image definition**

```python
cpu_image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("curl", "jq")
    .uv_pip_install("fastapi", "uvicorn[standard]", "websockets", "pydantic", "PyJWT")
    .add_local_dir(".", remote_path="/root")
)
```

The image is built once and cached by Modal. `add_local_dir` copies the entire repo into `/root` inside the container.

**2. `EnvConfig` dataclass**

```python
@dataclass
class EnvConfig:
    env_name: str
    app_name: str = "modal-template-fastapi"
    custom_domain: Optional[str] = None
    cpu_core_count: int = 1
    ram_memory_mib: int = 256
    gpu_type: Optional[str] = None
    server_hard_timeout_seconds: int = 150
    min_containers: int = 0
    secrets: list = field(default_factory=list)
    volumes: Dict[str, modal.Volume] = field(default_factory=lambda: FASTAPI_VOLUME)
```

`get_env_config(env_name)` validates and returns the right config. `build_fastapi_config(env)` translates it into a dict of Modal function kwargs.

---

### `src/main.py` — FastAPI Application Factory

Responsibilities:

- Creates the `FastAPI` app with title, description, and version
- Attaches `CORSMiddleware` allowing all origins (tighten in production)
- Registers a global `RequestValidationError` handler that returns a structured `ErrorDetail` with `VALIDATION_ERROR` code
- Mounts the API router under `/api/v1`
- Logs startup/shutdown via the `lifespan` context manager

---

### `src/api/routes.py` — Router

Five routes, two patterns:

| Pattern | Routes | Auth |
|---|---|---|
| Public | `GET /health` | None |
| Protected | `GET /items`, `POST /items`, `GET /items/{id}`, `PUT /items/{id}`, `DELETE /items/{id}` | `Depends(get_current_user)` |

Each route delegates business logic entirely to `handler.py` — the route layer only maps HTTP semantics (status codes, response models) onto handler return values.

---

### `src/api/auth.py` — JWT Dependency

```mermaid
flowchart LR
    A["HTTP Request"] --> B{"Authorization\nheader present?"}
    B -->|No| C["401 MISSING_TOKEN"]
    B -->|Yes| D{"jwt.decode()"}
    D -->|ExpiredSignatureError| E["401 TOKEN_EXPIRED"]
    D -->|InvalidTokenError| F["401 INVALID_TOKEN"]
    D -->|OK| G["dict payload\n→ route handler"]

    style C fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
    style E fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
    style F fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
    style G fill:#dcfce7,stroke:#15803d,color:#14532d
```

`JWT_SECRET` and `JWT_ALGORITHM` (default `HS256`) are read from environment variables at call time — not at import time — so they can be injected by Modal secrets or set in test fixtures.

---

### `src/api/handler.py` — Business Logic

An intentionally simple in-memory store using a module-level `dict`:

```python
_store: Dict[str, Dict[str, Any]] = {}
```

Each item is keyed by a UUID and stores all fields from the request `data` payload plus `id` and `project_id`. **This is the layer to replace** when wiring up a real database — the routes and models need zero changes.

---

### `src/api/models.py` — Pydantic Models

```mermaid
classDiagram
    class BaseResponse {
        +str session_id
    }
    class ItemResponse {
        +str status
        +str message
        +dict data
    }
    class HealthCheckResponse {
        +HealthStatus status
        +str service_name
        +str version
        +dict services_summary
    }
    class GenericRequest {
        +dict data
        +str project_id
    }
    class ErrorDetail {
        +str detail
        +str session_id
        +str error_code
    }
    class TokenPayload {
        +str sub
        +int exp
        +int iat
    }

    BaseResponse <|-- ItemResponse
    BaseResponse <|-- HealthCheckResponse
```

All responses carry a unique `session_id` (UUID v4 generated per request) for distributed tracing and support correlation.

---

## Environment Model

Three named environments, each with independent Modal app names, secrets, and scaling config:

| Field | `feat` | `dev` | `prod` |
|---|---|---|---|
| Modal app name | `modal-template-fastapi-feat` | `modal-template-fastapi-dev` | `modal-template-fastapi-prod` |
| Custom domain | `feat-app.modal.run` | `dev-app.modal.run` | `prod-app.modal.run` |
| CPU cores | 1 | 1 | 1 |
| RAM (MiB) | 256 | 256 | 256 |
| Min containers | 0 (scale to zero) | 0 (scale to zero) | **1** (always warm) |
| Secrets | `fastapi-auth-secrets` | `fastapi-auth-secrets` | `fastapi-auth-secrets` |
| Deploy trigger | `push feat/**` | `PR merged → dev` | `PR merged → production` |

!!! tip "Adding a new environment"
    1. Add a new `EnvConfig(env_name="staging", ...)` instance to `modal_common.py`
    2. Add it to `ENV_CONFIGS` dict
    3. Add a GitHub Actions variable (`vars.STAGING`) in the repo settings
    4. Add the routing condition to the `Set Modal environment` step in `modal-deploy.yml`
