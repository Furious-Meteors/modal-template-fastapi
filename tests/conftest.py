import os
import sys
import time
from types import SimpleNamespace

# Must be set before any src imports so auth._get_secret_key() resolves
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-at-least-32-bytes")

import jwt
import pytest
from fastapi.testclient import TestClient

# Provide a lightweight modal stub for local test runs where modal is not installed.
if "modal" not in sys.modules:
    class _DummyImage:
        def apt_install(self, *args, **kwargs):
            return self

        def uv_pip_install(self, *args, **kwargs):
            return self

        def add_local_dir(self, *args, **kwargs):
            return self

    class _DummyImageFactory:
        @staticmethod
        def debian_slim(*args, **kwargs):
            return _DummyImage()

    class _DummyVolume:
        @staticmethod
        def from_name(*args, **kwargs):
            return _DummyVolume()

    class _DummySecret:
        @staticmethod
        def from_name(*args, **kwargs):
            return {}

        @staticmethod
        def from_dict(*args, **kwargs):
            return {}

    sys.modules["modal"] = SimpleNamespace(
        Image=_DummyImageFactory,
        Volume=_DummyVolume,
        Secret=_DummySecret,
    )

from src.main import app
from src.api import handler
from src.api.auth import get_current_user

TEST_SECRET = "test-secret-key-for-pytest-at-least-32-bytes"
TEST_USER = {"sub": "test@example.com", "name": "Test User"}


def make_token(
    sub: str = "test@example.com",
    exp_offset: int = 3600,
    secret: str = TEST_SECRET,
    algorithm: str = "HS256",
) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "iat": now, "exp": now + exp_offset},
        secret,
        algorithm=algorithm,
    )


@pytest.fixture(autouse=True)
def clear_store():
    """Wipe the in-memory item store before and after every test."""
    handler._store.clear()
    yield
    handler._store.clear()


@pytest.fixture
def client():
    """TestClient with auth dependency overridden — use for item/business logic tests."""
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client():
    """TestClient with real JWT auth — use for auth-specific tests."""
    app.dependency_overrides.clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def valid_token() -> str:
    return make_token()


@pytest.fixture
def expired_token() -> str:
    return make_token(exp_offset=-3600)


@pytest.fixture
def wrong_secret_token() -> str:
    return make_token(secret="completely-wrong-secret-at-least-32-bytes")


@pytest.fixture
def auth_headers(valid_token: str) -> dict:
    return {"Authorization": f"Bearer {valid_token}"}
