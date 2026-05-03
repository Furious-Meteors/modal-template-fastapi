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
    detail = auth_client.get("/api/v1/items").json()["detail"]
    assert detail["error_code"] == "MISSING_TOKEN"


def test_malformed_bearer_value_returns_401(auth_client: TestClient):
    response = auth_client.get("/api/v1/items", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_malformed_bearer_error_code(auth_client: TestClient):
    detail = auth_client.get(
        "/api/v1/items", headers={"Authorization": "Bearer not-a-jwt"}
    ).json()["detail"]
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
    ).json()["detail"]
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
    ).json()["detail"]
    assert detail["error_code"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# Valid token
# ---------------------------------------------------------------------------


def test_valid_token_grants_access(auth_client: TestClient, auth_headers: dict):
    response = auth_client.get("/api/v1/items", headers=auth_headers)
    assert response.status_code == 200


def test_valid_token_on_post(auth_client: TestClient, auth_headers: dict):
    response = auth_client.post(
        "/api/v1/items", json={"data": {"name": "test"}}, headers=auth_headers
    )
    assert response.status_code == 201


def test_error_response_includes_session_id(auth_client: TestClient):
    detail = auth_client.get("/api/v1/items").json()["detail"]
    assert "session_id" in detail
    assert detail["session_id"]  # non-empty
