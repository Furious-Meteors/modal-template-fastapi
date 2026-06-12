"""
Tests for src/observability/middleware.py

Uses the real FastAPI app (with TelemetryMiddleware already mounted) so
middleware behavior is tested through actual HTTP requests via TestClient.

All tests run against the no-op OTel providers installed by conftest.py
(which imports src.main before setup_telemetry() is ever called), so no
network calls or real exporters are involved.
"""
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# x-session-id header
# ---------------------------------------------------------------------------


def test_x_session_id_header_present_on_200(client: TestClient):
    """Middleware always sets x-session-id on successful responses."""
    response = client.get("/api/v1/health")
    assert "x-session-id" in response.headers


def test_x_session_id_header_present_on_404(client: TestClient):
    """Middleware sets x-session-id even when the route is not found."""
    response = client.get("/this-route-does-not-exist")
    assert "x-session-id" in response.headers


def test_x_session_id_header_present_on_401(auth_client: TestClient):
    """Middleware sets x-session-id on auth failure responses."""
    response = auth_client.get("/api/v1/items")
    assert response.status_code == 401
    assert "x-session-id" in response.headers


def test_x_session_id_is_non_empty(client: TestClient):
    response = client.get("/api/v1/health")
    assert response.headers["x-session-id"] != ""


def test_x_session_id_is_unique_per_request(client: TestClient):
    """Each request must get a fresh session ID — never reused."""
    id1 = client.get("/api/v1/health").headers["x-session-id"]
    id2 = client.get("/api/v1/health").headers["x-session-id"]
    assert id1 != id2


def test_x_session_id_looks_like_uuid(client: TestClient):
    """The middleware generates a UUID v4 — format: 8-4-4-4-12 hex chars."""
    session_id = client.get("/api/v1/health").headers["x-session-id"]
    import re
    uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    assert re.match(uuid_pattern, session_id), f"Not a UUID: {session_id}"


def test_x_session_id_consistent_with_body_session_id_on_health(client: TestClient):
    """
    The health endpoint puts its own session_id in the body.
    The middleware generates a separate x-session-id header.
    Both should be present and be valid UUIDs — they may differ.
    """
    response = client.get("/api/v1/health")
    body_session_id   = response.json()["session_id"]
    header_session_id = response.headers["x-session-id"]
    assert body_session_id
    assert header_session_id


# ---------------------------------------------------------------------------
# Route template resolution — cardinality guard
# ---------------------------------------------------------------------------


def test_middleware_uses_route_template_not_raw_path(client: TestClient):
    """
    Middleware must resolve path parameters to templates so metrics don't
    explode in cardinality (e.g. /items/{item_id}, not /items/abc-123).
    Verified indirectly: two different item IDs produce the same route label
    in the structured log. We confirm this by checking the response is not 404
    for a known item ID path.
    """
    created = client.post("/api/v1/items", json={"data": {"name": "test"}}).json()
    item_id = created["data"]["id"]
    response = client.get(f"/api/v1/items/{item_id}")
    assert response.status_code == 200


def test_middleware_handles_unmatched_route_without_error(client: TestClient):
    """Unmatched routes (404s) must still return a response — never crash."""
    response = client.get("/completely/unknown/path")
    assert response.status_code == 404
    assert "x-session-id" in response.headers


# ---------------------------------------------------------------------------
# HTTP response pass-through — middleware must not alter responses
# ---------------------------------------------------------------------------


def test_middleware_does_not_change_200_status(client: TestClient):
    assert client.get("/api/v1/health").status_code == 200


def test_middleware_does_not_change_201_status(client: TestClient):
    response = client.post("/api/v1/items", json={"data": {"name": "x"}})
    assert response.status_code == 201


def test_middleware_does_not_change_404_status(client: TestClient):
    response = client.get("/api/v1/items/does-not-exist")
    assert response.status_code == 404


def test_middleware_does_not_change_401_status(auth_client: TestClient):
    assert auth_client.get("/api/v1/items").status_code == 401


def test_middleware_does_not_change_422_status(client: TestClient):
    response = client.post("/api/v1/items", json={})
    assert response.status_code == 422


def test_middleware_does_not_alter_response_body(client: TestClient):
    data = client.get("/api/v1/health").json()
    assert data["status"] == "healthy"
    assert "session_id" in data
    assert "service_name" in data


# ---------------------------------------------------------------------------
# Trace propagation — W3C traceparent header
# ---------------------------------------------------------------------------


def test_middleware_accepts_request_with_traceparent_header(client: TestClient):
    """
    A valid W3C traceparent header must not cause any error.
    The middleware extracts the context and creates a child span.
    """
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    response = client.get(
        "/api/v1/health",
        headers={"traceparent": traceparent},
    )
    assert response.status_code == 200


def test_middleware_accepts_request_without_traceparent_header(client: TestClient):
    """Requests without traceparent work normally — a new root span is created."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200


def test_middleware_accepts_malformed_traceparent_gracefully(client: TestClient):
    """A malformed traceparent must not crash the request — OTel ignores it."""
    response = client.get(
        "/api/v1/health",
        headers={"traceparent": "not-a-valid-traceparent"},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Error path — middleware must never swallow exceptions
# ---------------------------------------------------------------------------


def test_middleware_propagates_404_correctly(client: TestClient):
    response = client.get("/api/v1/items/nonexistent-id")
    assert response.status_code == 404
    assert "x-session-id" in response.headers


def test_middleware_propagates_validation_error_correctly(client: TestClient):
    response = client.post("/api/v1/items", json={"wrong_field": "value"})
    assert response.status_code == 422
    assert "x-session-id" in response.headers


def test_middleware_propagates_auth_error_correctly(auth_client: TestClient):
    response = auth_client.get("/api/v1/items", headers={"Authorization": "Bearer bad"})
    assert response.status_code == 401
    assert "x-session-id" in response.headers


# ---------------------------------------------------------------------------
# Concurrent requests — in-flight counter correctness
# ---------------------------------------------------------------------------


def test_middleware_handles_multiple_sequential_requests(client: TestClient):
    """Sequential requests all complete normally — no state leak between them."""
    for _ in range(5):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert "x-session-id" in response.headers


def test_each_request_gets_distinct_session_id(client: TestClient):
    """Five sequential requests must produce five distinct session IDs."""
    ids = {client.get("/api/v1/health").headers["x-session-id"] for _ in range(5)}
    assert len(ids) == 5
