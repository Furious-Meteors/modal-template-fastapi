# Testing

A comprehensive pytest suite covering auth, CRUD routes, and all Pydantic models — 79 tests, zero external dependencies, sub-second execution.

---

## Running Tests

```bash
# Install test dependencies (first time)
pip install -r .github/requirements/test.txt

# Run the full suite
pytest tests/ -v

# Run a specific file
pytest tests/test_auth.py -v

# Run with short traceback on failure
pytest tests/ --tb=short

# Show coverage (if pytest-cov is installed)
pytest tests/ --cov=src --cov-report=term-missing
```

---

## Test Suite Overview

| File | Scope | Tests |
|---|---|---|
| `tests/test_health.py` | Health endpoint — public route behaviour | 8 |
| `tests/test_auth.py` | JWT auth layer — all error paths + valid access | 12 |
| `tests/test_items.py` | Full CRUD lifecycle — create, read, list, update, delete | 30 |
| `tests/test_models.py` | Pydantic v2 model validation — required fields, type checks, constraints | 29 |
| **Total** | | **79** |

---

## Architecture of the Test Suite

### Two `TestClient` fixtures

The suite uses two distinct client fixtures defined in `conftest.py`:

| Fixture | Auth | When to use |
|---|---|---|
| `client` | Overridden — `get_current_user` returns a mock user dict | Business logic tests (items, health) |
| `auth_client` | Real JWT verification | Auth-specific tests |

This separation keeps item tests focused on CRUD correctness without the noise of JWT setup, while auth tests exercise the real middleware.

```python
# conftest.py — auth bypassed via FastAPI dependency override
@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()

# conftest.py — real JWT path
@pytest.fixture
def auth_client():
    app.dependency_overrides.clear()
    with TestClient(app) as c:
        yield c
```

### Store isolation

The handler uses a module-level `_store` dict. An `autouse` fixture wipes it before and after every single test:

```python
@pytest.fixture(autouse=True)
def clear_store():
    handler._store.clear()
    yield
    handler._store.clear()
```

This guarantees tests are **fully independent** — the order of execution never matters.

---

## `conftest.py` — Fixture Reference

### JWT helpers

```python
TEST_SECRET = "test-secret-key-for-pytest-at-least-32-bytes"
TEST_USER   = {"sub": "test@example.com", "name": "Test User"}

def make_token(sub, exp_offset=3600, secret=TEST_SECRET, algorithm="HS256") -> str:
    """Factory for generating JWTs with arbitrary parameters."""
```

| Fixture | Returns | Description |
|---|---|---|
| `valid_token` | `str` | JWT valid for 1 hour |
| `expired_token` | `str` | JWT with `exp` 1 hour in the past |
| `wrong_secret_token` | `str` | JWT signed with a different secret |
| `auth_headers` | `dict` | `{"Authorization": "Bearer <valid_token>"}` |

### Environment setup

`JWT_SECRET` is set via `os.environ.setdefault()` **before any `src` imports**, so `auth._get_secret_key()` always resolves — even in a CI environment with no secrets configured:

```python
# Top of conftest.py — must precede any src imports
import os
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-at-least-32-bytes")

from src.main import app      # safe — JWT_SECRET already set
from src.api import handler   # imported for store manipulation
```

---

## Test Modules in Depth

### `test_health.py` — 8 tests

Verifies the public health endpoint against the full response contract:

- Returns `200`
- `status` == `"healthy"`
- `service_name` == `"modal-template-fastapi"`, `version` == `"1.0.0"`
- `session_id` is present and non-empty
- `services_summary` contains `total`, `healthy`, `unhealthy` keys
- `session_id` is **unique** across multiple calls (UUID v4 generated per request)
- **No auth required** — accessible without any `Authorization` header

---

### `test_auth.py` — 12 tests

Tests the JWT middleware in isolation using `auth_client` (no dependency overrides):

=== "Missing / malformed tokens"

    - No `Authorization` header → `401` + `MISSING_TOKEN`
    - `Bearer not-a-jwt` → `401` + `INVALID_TOKEN`
    - `Basic <token>` (wrong scheme) → `401` + `MISSING_TOKEN`

=== "Invalid tokens"

    - Expired token → `401` + `TOKEN_EXPIRED`
    - Token signed with wrong secret → `401` + `INVALID_TOKEN`

=== "Valid token"

    - Valid token on `GET /items` → `200`
    - Valid token on `POST /items` → `201`
    - Error responses include a non-empty `session_id`

---

### `test_items.py` — 30 tests

Full CRUD lifecycle tests using the `client` fixture (auth bypassed). A `_create_item()` helper reduces setup boilerplate:

```python
def _create_item(client, data=None, project_id=None) -> dict:
    payload = {"data": data or {"name": "widget", "value": 1}}
    if project_id:
        payload["project_id"] = project_id
    return client.post("/api/v1/items", json=payload).json()
```

**Coverage checklist:**

- [x] `GET /items` returns `[]` on empty store
- [x] `GET /items` returns all created items
- [x] `POST /items` → `201`, `status == "created"`, auto-generated `id`, persists `data` and `project_id`
- [x] `POST /items` — `project_id` optional, empty `data` allowed, missing `data` → `422`
- [x] UUID `id` values are unique across separate creates
- [x] `GET /items/{id}` → `200` with `status == "success"`, correct item returned
- [x] `GET /items/{id}` — non-existent ID → `404` with ID in detail
- [x] `PUT /items/{id}` → `200`, `status == "updated"`, new data visible on subsequent `GET`
- [x] `PUT /items/{id}` — preserves `id`, missing `data` → `422`, non-existent → `404`
- [x] `DELETE /items/{id}` → `204`, item gone from store and from `GET /items`
- [x] `DELETE /items/{id}` — only removes the targeted item
- [x] `DELETE /items/{id}` — non-existent → `404`

---

### `test_models.py` — 29 tests

Pure Pydantic unit tests — no HTTP, no TestClient, instantaneous:

| Model | Tests | Key assertions |
|---|---|---|
| `GenericRequest` | 6 | `data` required; `project_id` optional; `min_length=1` enforced |
| `BaseResponse` | 2 | `session_id` required |
| `ItemResponse` | 4 | `status` and `message` required; `data` defaults to `None` |
| `HealthCheckResponse` | 4 | `services_summary` required; `HealthStatus` enum |
| `ErrorDetail` | 3 | `detail` required; `session_id` and `error_code` optional |
| `TokenPayload` | 4 | `sub` and `exp` required; `iat` optional |
| `FileUploadRequest` | 4 | `project_id` required with `min_length=1`; `filename` optional |
| `FileUploadResponse` | 2 | All optional fields (`file_id`, `file_size`, `file_path`) default to `None` |

---

## CI Integration

The test suite is run automatically by `app-testing.yml` on every pull request targeting `dev` or `production`, and also called as a prerequisite job by `modal-deploy.yml` before any deployment.

See the [CI/CD guide](cicd.md) for the full workflow.

!!! note "No secrets needed in CI"
    `conftest.py` sets a default `JWT_SECRET` before imports. The full test suite runs in GitHub Actions with zero secrets configured.
