import os
import time

# Must be set before any src imports so auth._get_secret_key() resolves.
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-at-least-32-bytes")

import jwt
import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.api.auth import get_current_user
from src.deps import get_item_service
from src.adapters.persistence.memory import InMemoryItemRepository
from src.core.services.item_service import ItemService

TEST_SECRET = "test-secret-key-for-pytest-at-least-32-bytes"
TEST_USER = {
    "sub": "test@example.com",
    "scopes": ["items:read", "items:write"],
}


def make_token(
    sub: str = "test@example.com",
    exp_offset: int = 3600,
    secret: str = TEST_SECRET,
    algorithm: str = "HS256",
    scopes: list[str] | None = None,
) -> str:
    now = int(time.time())
    payload: dict = {"sub": sub, "iat": now, "exp": now + exp_offset}
    if scopes is not None:
        payload["scopes"] = scopes
    return jwt.encode(payload, secret, algorithm=algorithm)


@pytest.fixture(autouse=True)
def fresh_item_service():
    """
    Each test gets an isolated in-memory repository — no state bleed between tests.
    Overrides the get_item_service FastAPI dependency for the duration of each test.
    """
    service = ItemService(InMemoryItemRepository())
    app.dependency_overrides[get_item_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_item_service, None)


@pytest.fixture
def client(fresh_item_service):
    """TestClient with auth dependency overridden — use for item/business logic tests."""
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def auth_client(fresh_item_service):
    """TestClient with real JWT auth — use for auth-specific tests."""
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def valid_token() -> str:
    """Valid token with no scopes — passes auth, fails scope checks."""
    return make_token()


@pytest.fixture
def read_token() -> str:
    return make_token(scopes=["items:read"])


@pytest.fixture
def write_token() -> str:
    return make_token(scopes=["items:read", "items:write"])


@pytest.fixture
def expired_token() -> str:
    return make_token(exp_offset=-3600)


@pytest.fixture
def wrong_secret_token() -> str:
    return make_token(secret="completely-wrong-secret-at-least-32-bytes")


@pytest.fixture
def auth_headers(valid_token: str) -> dict:
    return {"Authorization": f"Bearer {valid_token}"}


@pytest.fixture
def write_headers(write_token: str) -> dict:
    return {"Authorization": f"Bearer {write_token}"}
