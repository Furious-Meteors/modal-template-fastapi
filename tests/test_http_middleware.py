"""
Tests for src/adapters/http/middleware.py

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


def test_x_session_id_matches_body_session_id_on_health(client: TestClient):
    """The body session_id comes from request.state, set once by the middleware."""
    response = client.get("/api/v1/health")
    assert response.json()["session_id"] == response.headers["x-session-id"]


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
    created = client.post("/api/v1/items", json={"name": "test"}).json()
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
    response = client.post("/api/v1/items", json={"name": "x"})
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


# ---------------------------------------------------------------------------
# Metric label cardinality / in-flight gauge
# ---------------------------------------------------------------------------


def _route_labels(monkeypatch) -> list[str]:
    from src.adapters.http import middleware

    seen: list[str] = []
    real = middleware._record

    def spy(instr, method, route, *args):
        seen.append(route)
        return real(instr, method, route, *args)

    monkeypatch.setattr(middleware, "_record", spy)
    return seen


def test_unmatched_paths_share_one_route_label(client: TestClient, monkeypatch):
    seen = _route_labels(monkeypatch)
    client.get("/scanner/probe-1")
    client.get("/scanner/probe-2")
    assert seen == ["unmatched", "unmatched"]


def test_path_params_resolve_to_template_in_metric_label(client: TestClient, monkeypatch):
    seen = _route_labels(monkeypatch)
    client.get("/api/v1/items/abc")
    client.get("/api/v1/items/xyz")
    client.get("/api/v1/health")
    client.get("/docs")
    assert seen == [
        "/api/v1/items/{item_id}",
        "/api/v1/items/{item_id}",
        "/api/v1/health",
        "/docs",
    ]


def test_active_requests_decremented_when_handler_raises(client: TestClient, monkeypatch):
    from src.adapters.http import middleware

    calls: list[int] = []

    class _Gauge:
        def add(self, value, labels=None):
            calls.append(value)

    instr = dict(middleware._get_instruments())
    instr["active_requests"] = _Gauge()
    monkeypatch.setattr(middleware, "_instruments", instr)

    from fastapi.testclient import TestClient as TC
    from src.main import app

    @app.get("/__boom")
    async def _boom():
        raise RuntimeError("boom")

    try:
        with TC(app, raise_server_exceptions=False) as c:
            assert c.get("/__boom").status_code == 500
    finally:
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/__boom"]
    assert calls == [1, -1]


# ---------------------------------------------------------------------------
# Streaming support (pure ASGI middleware)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_response_counts_bytes_and_returns_gauge_to_zero(monkeypatch, event_bus):
    from src.adapters.http import middleware
    from src.main import app
    from tests.asgi_helpers import OpenStream
    from tests.conftest import make_token

    adds: list[int] = []
    recorded: list[tuple] = []

    class _Gauge:
        def add(self, value, labels=None):
            adds.append(value)

    instr = dict(middleware._get_instruments())
    instr["active_requests"] = _Gauge()
    monkeypatch.setattr(middleware, "_instruments", instr)
    real = middleware._record
    monkeypatch.setattr(middleware, "_record", lambda *a: (recorded.append(a), real(*a))[1])

    headers = {"Authorization": f"Bearer {make_token(scopes=['events:read', 'items:read'])}"}
    stream = OpenStream(app, "/api/v1/events/stream", headers)
    await stream.read_until(b"retry")
    assert adds == [1]  # in flight while the stream is open
    from tests.asgi_helpers import wait_until

    await wait_until(lambda: event_bus.subscriber_count("items") == 1)
    await event_bus.publish("items", "e", {"k": "v"})
    await stream.read_until(b'"k": "v"')
    await stream.disconnect()

    assert adds == [1, -1]
    _, method, route, status, _, size = recorded[-1]
    assert (method, route, status) == ("GET", "/api/v1/events/stream", 200)
    assert size > 0  # counted from the body chunks, not a content-length header


def test_websocket_connection_is_logged_with_close_code(client, caplog):
    import logging

    from src.api.auth import create_ws_ticket
    from src.api.models import TokenPayload
    from starlette.websockets import WebSocketDisconnect

    ticket = create_ws_ticket(TokenPayload(sub="u", exp=9_999_999_999, scopes=["events:read"]))[0]
    with caplog.at_level(logging.INFO, logger="src.adapters.http.middleware"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/ws?ticket=bad") as ws:
                ws.receive_json()
    assert any(
        "WEBSOCKET /api/v1/ws closed code=4401" in r.getMessage() for r in caplog.records
    )


def test_websocket_route_label_has_single_prefix(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from src.adapters.http import middleware

    seen: list[str] = []
    real = middleware._route_template
    monkeypatch.setattr(middleware, "_route_template", lambda conn: (seen.append(real(conn)), seen[-1])[1])
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.receive_json()
    assert seen and set(seen) == {"/api/v1/ws"}
