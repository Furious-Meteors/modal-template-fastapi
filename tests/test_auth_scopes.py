"""
Tests for scope-based authorisation on write endpoints.

Read endpoints (GET) require the "items:read" scope.
Write endpoints (POST, PUT, DELETE) require the "items:write" scope.
"""
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Read endpoints — require "items:read" scope
# ---------------------------------------------------------------------------


def test_list_items_requires_read_scope(auth_client: TestClient, auth_headers: dict):
    """GET /items returns 403 when the token has no scopes."""
    response = auth_client.get("/api/v1/items", headers=auth_headers)
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "INSUFFICIENT_SCOPE"


def test_list_items_succeeds_with_read_scope(auth_client: TestClient, read_headers: dict):
    response = auth_client.get("/api/v1/items", headers=read_headers)
    assert response.status_code == 200


def test_get_item_requires_read_scope(auth_client: TestClient, auth_headers: dict):
    """GET /items/{id} returns 403 (not 404) for a token with no scopes."""
    response = auth_client.get("/api/v1/items/nonexistent", headers=auth_headers)
    assert response.status_code == 403


def test_get_item_succeeds_with_read_scope(auth_client: TestClient, read_headers: dict):
    """With items:read the request reaches the handler (404 for a missing item)."""
    response = auth_client.get("/api/v1/items/nonexistent", headers=read_headers)
    assert response.status_code == 404


def test_write_only_token_cannot_read(auth_client: TestClient):
    from tests.conftest import make_token
    headers = {"Authorization": f"Bearer {make_token(scopes=['items:write'])}"}
    assert auth_client.get("/api/v1/items", headers=headers).status_code == 403


# ---------------------------------------------------------------------------
# Write endpoints — require "items:write" scope
# ---------------------------------------------------------------------------


def test_create_requires_write_scope(auth_client: TestClient, auth_headers: dict):
    """POST /items returns 403 when token has no scopes."""
    response = auth_client.post("/api/v1/items", json={"name": "x"}, headers=auth_headers)
    assert response.status_code == 403


def test_create_requires_write_scope_error_code(auth_client: TestClient, auth_headers: dict):
    response = auth_client.post("/api/v1/items", json={"name": "x"}, headers=auth_headers)
    assert response.json()["detail"]["error_code"] == "INSUFFICIENT_SCOPE"


def test_create_succeeds_with_write_scope(auth_client: TestClient, write_headers: dict):
    """POST /items returns 201 when token includes items:write."""
    response = auth_client.post("/api/v1/items", json={"name": "scoped"}, headers=write_headers)
    assert response.status_code == 201


def test_update_requires_write_scope(auth_client: TestClient, auth_headers: dict, write_headers: dict):
    item_id = auth_client.post("/api/v1/items", json={"name": "x"}, headers=write_headers).json()["data"]["id"]
    response = auth_client.put(f"/api/v1/items/{item_id}", json={"name": "y"}, headers=auth_headers)
    assert response.status_code == 403


def test_update_succeeds_with_write_scope(auth_client: TestClient, write_headers: dict):
    item_id = auth_client.post("/api/v1/items", json={"name": "x"}, headers=write_headers).json()["data"]["id"]
    response = auth_client.put(f"/api/v1/items/{item_id}", json={"name": "updated"}, headers=write_headers)
    assert response.status_code == 200


def test_delete_requires_write_scope(auth_client: TestClient, auth_headers: dict, write_headers: dict):
    item_id = auth_client.post("/api/v1/items", json={"name": "x"}, headers=write_headers).json()["data"]["id"]
    response = auth_client.delete(f"/api/v1/items/{item_id}", headers=auth_headers)
    assert response.status_code == 403


def test_delete_succeeds_with_write_scope(auth_client: TestClient, write_headers: dict):
    item_id = auth_client.post("/api/v1/items", json={"name": "x"}, headers=write_headers).json()["data"]["id"]
    response = auth_client.delete(f"/api/v1/items/{item_id}", headers=write_headers)
    assert response.status_code == 204


def test_read_scope_insufficient_for_write(auth_client: TestClient, read_token: str):
    """A token with only items:read cannot write."""
    headers = {"Authorization": f"Bearer {read_token}"}
    response = auth_client.post("/api/v1/items", json={"name": "x"}, headers=headers)
    assert response.status_code == 403
