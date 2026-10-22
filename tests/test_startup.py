"""
Tests for fail-fast startup validation (app lifespan).

Entering TestClient as a context manager runs the lifespan, so a bad config
must raise there rather than on the first request.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from src import deps
from src.main import app


@pytest.fixture(autouse=True)
def clean_repo_cache():
    deps._get_repository.cache_clear()
    yield
    deps._get_repository.cache_clear()


def test_startup_succeeds_with_defaults():
    with TestClient(app):
        pass


def test_startup_fails_on_unknown_backend(monkeypatch):
    monkeypatch.setenv("PERSISTENCE_BACKEND", "nope")
    with pytest.raises(ValueError, match="Unknown PERSISTENCE_BACKEND"):
        with TestClient(app):
            pass


def test_startup_fails_when_backend_env_var_missing(monkeypatch):
    monkeypatch.setitem(deps._REQUIRED_ENV, "needs_url", ["SOME_URL"])
    monkeypatch.setenv("PERSISTENCE_BACKEND", "needs_url")
    monkeypatch.delenv("SOME_URL", raising=False)
    with pytest.raises(EnvironmentError, match="SOME_URL"):
        with TestClient(app):
            pass


def test_startup_fails_without_jwt_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        with TestClient(app):
            pass


def test_startup_fails_with_short_jwt_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        with TestClient(app):
            pass


def test_memory_backend_in_prod_logs_warning(monkeypatch, caplog):
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("MODAL_ENV", "prod")
    monkeypatch.setenv("ALLOW_MEMORY_IN_PROD", "true")  # explicit opt-in: warns instead of refusing
    with caplog.at_level(logging.WARNING, logger="src.deps"):
        deps.init_dependencies()
    assert any("memory in prod" in r.message for r in caplog.records)


def test_memory_backend_in_dev_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("MODAL_ENV", "dev")
    with caplog.at_level(logging.WARNING, logger="src.deps"):
        deps.init_dependencies()
    assert not caplog.records
