"""Tests for JWT authentication on protected endpoints."""
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_SECRET, make_token


# ---------------------------------------------------------------------------
# Missing / malformed token
# ---------------------------------------------------------------------------


def test_no_token_returns_401(auth_client: TestClient):
    response = auth_client.get("/api/v1/items")
    assert response.status_code == 401


def test_no_token_error_code(auth_client: TestClient):
    detail = auth_client.get("/api/v1/items").json()
    assert detail["error_code"] == "MISSING_TOKEN"


def test_malformed_bearer_value_returns_401(auth_client: TestClient):
    response = auth_client.get("/api/v1/items", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_malformed_bearer_error_code(auth_client: TestClient):
    detail = auth_client.get(
        "/api/v1/items", headers={"Authorization": "Bearer not-a-jwt"}
    ).json()
    assert detail["error_code"] == "INVALID_TOKEN"


def test_wrong_scheme_returns_401(auth_client: TestClient):
    token = make_token()
    response = auth_client.get("/api/v1/items", headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Expired token
# ---------------------------------------------------------------------------


def test_expired_token_returns_401(auth_client: TestClient, expired_token: str):
    response = auth_client.get(
        "/api/v1/items", headers={"Authorization": f"Bearer {expired_token}"}
    )
    assert response.status_code == 401


def test_expired_token_error_code(auth_client: TestClient, expired_token: str):
    detail = auth_client.get(
        "/api/v1/items", headers={"Authorization": f"Bearer {expired_token}"}
    ).json()
    assert detail["error_code"] == "TOKEN_EXPIRED"


# ---------------------------------------------------------------------------
# Wrong secret
# ---------------------------------------------------------------------------


def test_wrong_secret_returns_401(auth_client: TestClient, wrong_secret_token: str):
    response = auth_client.get(
        "/api/v1/items", headers={"Authorization": f"Bearer {wrong_secret_token}"}
    )
    assert response.status_code == 401


def test_wrong_secret_error_code(auth_client: TestClient, wrong_secret_token: str):
    detail = auth_client.get(
        "/api/v1/items", headers={"Authorization": f"Bearer {wrong_secret_token}"}
    ).json()
    assert detail["error_code"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# Valid token
# ---------------------------------------------------------------------------


def test_valid_token_grants_access(auth_client: TestClient, read_headers: dict):
    response = auth_client.get("/api/v1/items", headers=read_headers)
    assert response.status_code == 200


def test_valid_token_on_post(auth_client: TestClient, write_headers: dict):
    """POST requires items:write scope — a write-scoped token must succeed."""
    response = auth_client.post(
        "/api/v1/items", json={"name": "test"}, headers=write_headers
    )
    assert response.status_code == 201


def test_error_response_includes_session_id(auth_client: TestClient):
    detail = auth_client.get("/api/v1/items").json()
    assert "session_id" in detail
    assert detail["session_id"]  # non-empty


# ---------------------------------------------------------------------------
# Hardened claim validation
# ---------------------------------------------------------------------------


def _get(auth_client: TestClient, token: str):
    return auth_client.get("/api/v1/items", headers={"Authorization": f"Bearer {token}"})


def test_token_without_exp_is_rejected(auth_client: TestClient):
    token = make_token(scopes=["items:read"], include_exp=False)
    response = _get(auth_client, token)
    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


def test_token_without_sub_is_rejected(auth_client: TestClient):
    token = make_token(scopes=["items:read"], include_sub=False)
    response = _get(auth_client, token)
    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


def test_token_with_malformed_scopes_claim_is_rejected(auth_client: TestClient):
    token = make_token(extra_claims={"scopes": "items:read"})
    response = _get(auth_client, token)
    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


def test_audience_not_enforced_when_unconfigured(auth_client: TestClient, monkeypatch):
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    token = make_token(scopes=["items:read"])
    assert _get(auth_client, token).status_code == 200


def test_audience_enforced_when_configured(auth_client: TestClient, monkeypatch):
    monkeypatch.setenv("JWT_AUDIENCE", "my-api")
    good = make_token(scopes=["items:read"], extra_claims={"aud": "my-api"})
    wrong = make_token(scopes=["items:read"], extra_claims={"aud": "other-api"})
    missing = make_token(scopes=["items:read"])
    assert _get(auth_client, good).status_code == 200
    assert _get(auth_client, wrong).status_code == 401
    assert _get(auth_client, missing).status_code == 401


def test_issuer_enforced_when_configured(auth_client: TestClient, monkeypatch):
    monkeypatch.setenv("JWT_ISSUER", "https://issuer.example")
    good = make_token(scopes=["items:read"], extra_claims={"iss": "https://issuer.example"})
    wrong = make_token(scopes=["items:read"], extra_claims={"iss": "https://evil.example"})
    missing = make_token(scopes=["items:read"])
    assert _get(auth_client, good).status_code == 200
    assert _get(auth_client, wrong).status_code == 401
    assert _get(auth_client, missing).status_code == 401


# ---------------------------------------------------------------------------
# validate_auth_config() — startup check
# ---------------------------------------------------------------------------


def test_validate_auth_config_passes_with_strong_secret():
    from src.api.auth import validate_auth_config
    validate_auth_config()  # conftest sets a >=32 char secret


def test_validate_auth_config_rejects_missing_secret(monkeypatch):
    from src.api.auth import validate_auth_config
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        validate_auth_config()


def test_validate_auth_config_rejects_short_secret(monkeypatch):
    from src.api.auth import validate_auth_config
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="at least 32"):
        validate_auth_config()


# ---------------------------------------------------------------------------
# scripts/jwt_token_generator.py
# ---------------------------------------------------------------------------


def test_token_generator_honours_scopes(monkeypatch, capsys):
    import pathlib
    import runpy
    import sys

    path = pathlib.Path(__file__).parent.parent / "scripts" / "jwt_token_generator.py"
    monkeypatch.setattr(
        sys, "argv", ["gen", "--secret", TEST_SECRET, "--scopes", "items:read, other:scope"]
    )
    runpy.run_path(str(path), run_name="__main__")
    token = capsys.readouterr().out.strip()
    claims = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
    assert claims["scopes"] == ["items:read", "other:scope"]


# ---------------------------------------------------------------------------
# Algorithm allowlist
# ---------------------------------------------------------------------------


def test_validate_auth_config_rejects_unsupported_algorithm(monkeypatch):
    from src.api.auth import validate_auth_config

    monkeypatch.setenv("JWT_ALGORITHM", "none")
    with pytest.raises(RuntimeError, match="JWT_ALGORITHM"):
        validate_auth_config()


def test_validate_auth_config_accepts_hs512(monkeypatch):
    from src.api.auth import validate_auth_config

    monkeypatch.setenv("JWT_ALGORITHM", "HS512")
    validate_auth_config()
