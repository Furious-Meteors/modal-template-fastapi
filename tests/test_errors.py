"""
Tests for the unified error response shape and consistent session IDs.

Every error — auth, scope, validation, domain, not-found, wrong method,
unexpected failure — must be the same flat ErrorDetail body, and its
session_id must equal the x-session-id response header.
"""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.adapters.http.middleware import TelemetryMiddleware
from src.api.errors import AppError, get_session_id, register_exception_handlers

BASE_KEYS = {"detail", "session_id", "error_code"}


def _assert_flat_error(response, status: int, code: str):
    assert response.status_code == status
    body = response.json()
    assert isinstance(body["detail"], str)
    assert body["error_code"] == code
    assert body["session_id"]
    assert BASE_KEYS <= set(body)
    assert body["session_id"] == response.headers["x-session-id"]
    return body


# ---------------------------------------------------------------------------
# One shape everywhere
# ---------------------------------------------------------------------------


def test_missing_token_shape(auth_client: TestClient):
    r = auth_client.get("/api/v1/items")
    _assert_flat_error(r, 401, "MISSING_TOKEN")
    assert r.headers["www-authenticate"] == "Bearer"


def test_invalid_token_keeps_www_authenticate(auth_client: TestClient):
    r = auth_client.get("/api/v1/items", headers={"Authorization": "Bearer garbage"})
    _assert_flat_error(r, 401, "INVALID_TOKEN")
    assert r.headers["www-authenticate"] == "Bearer"


def test_insufficient_scope_shape(auth_client: TestClient, auth_headers: dict):
    r = auth_client.get("/api/v1/items", headers=auth_headers)
    body = _assert_flat_error(r, 403, "INSUFFICIENT_SCOPE")
    assert "items:read" in body["detail"]


def test_item_not_found_shape(client: TestClient):
    r = client.get("/api/v1/items/ghost-id")
    body = _assert_flat_error(r, 404, "ITEM_NOT_FOUND")
    assert "ghost-id" in body["detail"]


def test_update_and_delete_not_found_shape(client: TestClient):
    _assert_flat_error(client.put("/api/v1/items/x", json={"name": "n"}), 404, "ITEM_NOT_FOUND")
    _assert_flat_error(client.delete("/api/v1/items/x"), 404, "ITEM_NOT_FOUND")


def test_duplicate_name_shape(client: TestClient):
    client.post("/api/v1/items", json={"name": "dup"})
    _assert_flat_error(client.post("/api/v1/items", json={"name": "dup"}), 409, "ITEM_NAME_EXISTS")


def test_unmatched_route_shape(client: TestClient):
    _assert_flat_error(client.get("/api/v1/does-not-exist"), 404, "NOT_FOUND")


def test_wrong_method_shape(client: TestClient):
    _assert_flat_error(client.patch("/api/v1/health"), 405, "METHOD_NOT_ALLOWED")


def test_non_error_responses_have_no_error_fields(client: TestClient):
    body = client.get("/api/v1/health").json()
    assert "error_code" not in body


# ---------------------------------------------------------------------------
# Structured validation errors
# ---------------------------------------------------------------------------


def test_validation_error_lists_fields(client: TestClient):
    r = client.post("/api/v1/items", json={})
    body = _assert_flat_error(r, 422, "VALIDATION_ERROR")
    assert body["detail"] == "Request validation failed"
    fields = {e["field"]: e for e in body["errors"]}
    assert "body.name" in fields
    assert fields["body.name"]["type"] == "missing"
    assert fields["body.name"]["message"]


def test_validation_error_reports_query_params(client: TestClient):
    r = client.get("/api/v1/items", params={"limit": 0, "offset": -1})
    body = _assert_flat_error(r, 422, "VALIDATION_ERROR")
    assert {e["field"] for e in body["errors"]} == {"query.limit", "query.offset"}


def test_validation_error_is_not_a_stringified_exception(client: TestClient):
    body = client.post("/api/v1/items", json={}).json()
    assert "ValidationError" not in body["detail"]
    assert "[{" not in body["detail"]


# ---------------------------------------------------------------------------
# Session IDs
# ---------------------------------------------------------------------------


def test_success_body_session_id_matches_header(client: TestClient):
    r = client.post("/api/v1/items", json={"name": "s"})
    assert r.status_code == 201
    assert r.json()["session_id"] == r.headers["x-session-id"]


@pytest.mark.parametrize("path", ["/api/v1/health", "/api/v1/items"])
def test_session_id_differs_between_requests(client: TestClient, path: str):
    a = client.get(path).headers["x-session-id"]
    b = client.get(path).headers["x-session-id"]
    assert a != b


# ---------------------------------------------------------------------------
# Handler behaviour on a standalone app
# ---------------------------------------------------------------------------


def _mini_app(with_middleware: bool = True) -> FastAPI:
    app = FastAPI()
    if with_middleware:
        app.add_middleware(TelemetryMiddleware)
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    @app.get("/app-error")
    async def app_error():
        raise AppError(418, "TEAPOT", "short and stout", headers={"X-Custom": "1"})

    @app.get("/sid")
    async def sid(request_sid: str = Depends(get_session_id)):
        return {"session_id": request_sid}

    return app


def test_unhandled_exception_returns_flat_500_without_leaking_details():
    with TestClient(_mini_app(), raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error_code"] == "INTERNAL_ERROR"
    assert body["detail"] == "Internal server error"
    assert "secret internal detail" not in r.text
    assert body["session_id"] == r.headers["x-session-id"]


def test_app_error_status_code_and_headers():
    with TestClient(_mini_app()) as c:
        r = c.get("/app-error")
    assert r.status_code == 418
    assert r.json()["error_code"] == "TEAPOT"
    assert r.headers["x-custom"] == "1"


def test_get_session_id_falls_back_when_middleware_absent():
    with TestClient(_mini_app(with_middleware=False)) as c:
        r = c.get("/sid")
    assert r.json()["session_id"]


def test_storage_unavailable_maps_to_503_with_retry_after(client, fresh_item_service):
    from src.core.domain.errors import StorageUnavailableError

    async def boom(*a, **k):
        raise StorageUnavailableError("CannotConnectNowError")

    fresh_item_service.list = boom
    r = client.get("/api/v1/items")
    assert r.status_code == 503
    body = r.json()
    assert body["error_code"] == "STORAGE_UNAVAILABLE"
    assert r.headers["retry-after"] == "5"
    assert "CannotConnectNow" not in r.text  # driver details never reach clients
    assert body["session_id"] == r.headers["x-session-id"]
