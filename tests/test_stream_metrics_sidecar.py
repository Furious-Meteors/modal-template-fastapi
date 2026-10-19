"""
Stream and webhook metrics, recorded by TelemetryMiddleware from the wire.

The endpoints contain no telemetry code; these tests prove the sidecar observes
everything it used to be told, with the same bounded label sets.
"""
import asyncio
import json
import re
import time
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.websockets import WebSocketDisconnect

from src.adapters.telemetry import stream_metrics
from src.api import ratelimit
from src.api.auth import create_ws_ticket
from src.api.models import TokenPayload
from src.api.ratelimit import TokenBucketLimiter
from src.api.webhooks import sign
from src.main import app
from tests.asgi_helpers import OpenStream, wait_until
from tests.conftest import make_token

ROOT = Path(__file__).resolve().parent.parent


class Metrics:
    def __init__(self):
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])
        self.sm = stream_metrics.StreamMetrics(self.provider.get_meter("test"))

    def points(self, name):
        data = self.reader.get_metrics_data()
        return [p for rm in (data.resource_metrics if data else []) for sm in rm.scope_metrics
                for m in sm.metrics if m.name == name for p in m.data.data_points]

    def count(self, name, **labels):
        return sum(int(p.value) for p in self.points(name)
                   if all(dict(p.attributes).get(k) == v for k, v in labels.items()))

    def series(self, name):
        return {tuple(sorted(dict(p.attributes).items())) for p in self.points(name)}


@pytest.fixture
def m(monkeypatch):
    metrics = Metrics()
    monkeypatch.setattr(stream_metrics, "_instance", metrics.sm)
    return metrics


def eventually(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not fn():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def _bearer(*scopes):
    return {"Authorization": f"Bearer {make_token(scopes=list(scopes))}"}


SSE = "/api/v1/events/stream"
WS = "/api/v1/ws"
READ = ("events:read", "items:read", "webhooks:read")


def _ticket(*scopes):
    return create_ws_ticket(TokenPayload(sub="u", exp=9_999_999_999, scopes=list(scopes or READ)))[0]


# ── SSE ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sse_open_messages_and_client_disconnect(m, event_bus):
    stream = OpenStream(app, SSE, _bearer(*READ))
    await stream.read_until(b"retry")
    assert m.count("stream.connections.active", transport="sse") == 1
    await wait_until(lambda: event_bus.subscriber_count("items") == 1)
    await event_bus.publish("items", "item.created", {"id": "1"})
    await stream.read_until(b"item.created")
    assert m.count("stream.messages.sent", transport="sse") == 2  # the retry hint + one event

    await stream.disconnect()
    assert m.count("stream.connections.active", transport="sse") == 0
    assert m.count("stream.connections.closed", transport="sse", reason="client_disconnect") == 1


@pytest.mark.asyncio
async def test_sse_that_ends_itself_is_server_closed_and_counts_heartbeats(m, monkeypatch):
    monkeypatch.setattr("src.api.events.STREAM_HEARTBEAT_SECONDS", 0.03)
    monkeypatch.setattr("src.api.events.STREAM_MAX_DURATION_SECONDS", 0.2)
    stream = OpenStream(app, SSE, _bearer(*READ))
    await stream.body_until_end()
    await asyncio.wait_for(stream.task, 2)
    assert m.count("stream.connections.closed", transport="sse", reason="server_closed") == 1
    assert m.count("stream.messages.sent", transport="sse") >= 3  # retry + heartbeats
    assert m.count("stream.connections.active", transport="sse") == 0


@pytest.mark.asyncio
async def test_rejected_sse_requests_are_not_counted_as_streams(m, reset_stream_state):
    from src.api import streaming

    forbidden = OpenStream(app, SSE, _bearer("events:read"))  # lacks items:read
    await forbidden.wait_started()
    assert forbidden.status == 403
    await forbidden.body_until_end()

    streaming.limiter = streaming.StreamLimiter(0)
    busy = OpenStream(app, SSE, _bearer(*READ))
    await busy.wait_started()
    assert busy.status == 503
    await busy.body_until_end()

    assert m.points("stream.connections.active") == []
    assert m.points("stream.connections.closed") == []


# ── WebSocket ───────────────────────────────────────────────────────────────

def test_ws_messages_and_client_disconnect(m, client):
    with client.websocket_connect(f"{WS}?ticket={_ticket()}") as ws:
        assert m.count("stream.connections.active", transport="websocket") == 1
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        client.post("/api/v1/items", json={"name": "counted"})
        assert ws.receive_json()["event"] == "item.created"
    eventually(lambda: m.count("stream.connections.closed", transport="websocket", reason="client_disconnect") == 1)
    assert m.count("stream.messages.sent", transport="websocket") == 2  # the pong and the event
    assert m.count("stream.connections.active", transport="websocket") == 0


def _expect_close(client, url):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url) as ws:
            ws.receive_json()
    return exc.value.code


@pytest.mark.parametrize("scenario,reason", [
    ("bad_ticket", "rejected"),
    ("no_scope", "rejected"),
    ("unknown_topic", "rejected"),
    ("too_large", "message_too_large"),
    ("flood", "rate_limited"),
    ("max_duration", "max_duration"),
    ("capacity", "server_busy"),
])
def test_ws_close_reasons_come_from_close_codes(m, client, monkeypatch, scenario, reason):
    from src.api import streaming

    if scenario == "bad_ticket":
        assert _expect_close(client, f"{WS}?ticket=garbage") == 4401
    elif scenario == "no_scope":
        assert _expect_close(client, f"{WS}?ticket={_ticket('events:read')}") == 4403
    elif scenario == "unknown_topic":
        assert _expect_close(client, f"{WS}?ticket={_ticket()}&topic=nope") == 4400
    elif scenario == "too_large":
        monkeypatch.setattr("src.api.events.WS_MAX_MESSAGE_BYTES", 8)
        with client.websocket_connect(f"{WS}?ticket={_ticket()}") as ws:
            ws.send_text("x" * 100)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    elif scenario == "flood":
        monkeypatch.setattr(ratelimit, "new_ws_message_limiter", lambda: TokenBucketLimiter(0.001, burst=1))
        with client.websocket_connect(f"{WS}?ticket={_ticket()}") as ws:
            ws.send_json({"type": "ping"}); ws.receive_json()
            ws.send_json({"type": "ping"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    elif scenario == "max_duration":
        monkeypatch.setattr("src.api.events.STREAM_HEARTBEAT_SECONDS", 0.03)
        monkeypatch.setattr("src.api.events.STREAM_MAX_DURATION_SECONDS", 0.15)
        with client.websocket_connect(f"{WS}?ticket={_ticket()}") as ws:
            with pytest.raises(WebSocketDisconnect):
                while True:
                    ws.receive_json()
    elif scenario == "capacity":
        streaming.limiter = streaming.StreamLimiter(0)
        assert _expect_close(client, f"{WS}?ticket={_ticket()}") == 1013

    eventually(lambda: m.count("stream.connections.closed", transport="websocket", reason=reason) == 1)
    assert m.count("stream.connections.active", transport="websocket") == 0


# ── webhooks ────────────────────────────────────────────────────────────────

SECRET = "whsec-test-secret-value"
BODY = json.dumps({"a": 1}).encode()


@pytest.fixture
def hook(client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_ACME", SECRET)

    def send(body=BODY, *, wid="evt-1", ts=None, secret=SECRET, sig=None, drop=(), provider="acme"):
        ts = str(int(time.time()) if ts is None else ts)
        headers = {"x-webhook-id": wid, "x-webhook-timestamp": ts,
                   "x-webhook-signature": sig or sign(secret.encode(), ts, body)}
        for h in drop:
            headers.pop(h)
        return client.post(f"/api/v1/webhooks/{provider}", content=body, headers=headers)

    return send


def _outcomes(m):
    return {dict(p.attributes)["outcome"]: int(p.value) for p in m.points("webhook.received")}


def test_webhook_outcomes_are_derived_from_status_and_error_code(m, hook, monkeypatch):
    assert hook(wid="a").status_code == 202                                      # accepted
    assert hook(wid="a").status_code == 200                                      # duplicate
    assert hook(wid="b", secret="wrong").status_code == 401                      # bad_signature
    assert hook(wid="c", drop=("x-webhook-signature",)).status_code == 401       # bad_signature
    assert hook(wid="d", ts=int(time.time()) - 3600).status_code == 400          # stale
    assert hook(wid="e", drop=("x-webhook-timestamp",)).status_code == 400       # bad_timestamp
    assert hook(wid="f", drop=("x-webhook-id",)).status_code == 400              # bad_request
    assert hook(b"[1]", wid="g").status_code == 400                              # bad_request
    monkeypatch.setattr("src.api.webhooks.WEBHOOK_MAX_BODY_BYTES", 3)
    assert hook(wid="h").status_code == 413                                      # too_large
    monkeypatch.setattr("src.api.webhooks.WEBHOOK_MAX_BODY_BYTES", 1_048_576)
    ratelimit.webhook_limiter = TokenBucketLimiter(0.001, burst=0)
    assert hook(wid="i").status_code == 429                                      # rate_limited

    assert _outcomes(m) == {
        "accepted": 1, "duplicate": 1, "bad_signature": 2, "stale": 1,
        "bad_timestamp": 1, "bad_request": 2, "too_large": 1, "rate_limited": 1,
    }
    assert {dict(p.attributes)["provider"] for p in m.points("webhook.received")} == {"acme"}


def test_a_failed_publish_is_recorded_as_an_error(m, hook, event_bus, monkeypatch):
    from fastapi.testclient import TestClient

    async def boom(*a, **k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(event_bus, "publish", boom)
    ts = str(int(time.time()))
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/webhooks/acme", content=BODY, headers={
            "x-webhook-id": "x", "x-webhook-timestamp": ts, "x-webhook-signature": sign(SECRET.encode(), ts, BODY)})
    assert r.status_code == 500
    assert _outcomes(m) == {"error": 1}


def test_unknown_providers_and_wrong_methods_never_create_series(m, client):
    """path_params is attacker-controlled; labelling by it would be unbounded cardinality."""
    for i in range(40):
        client.post(f"/api/v1/webhooks/probe-{i}", content=BODY)   # 404 unknown provider
    client.get("/api/v1/webhooks/acme")                            # 405
    client.post("/api/v1/webhooks/ACME", content=BODY)             # malformed name -> 404
    assert m.points("webhook.received") == []


def test_provider_label_is_bounded_to_configured_providers(m, hook, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_OTHER", SECRET)
    hook(wid="1")
    hook(wid="1", provider="other")
    for i in range(30):
        hook(wid=str(i), provider=f"nobody-{i}")
    assert {dict(p.attributes)["provider"] for p in m.points("webhook.received")} == {"acme", "other"}


# ── the endpoints really are free of telemetry ──────────────────────────────

def test_api_layer_contains_no_telemetry_code():
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:opentelemetry|src\.infrastructure|src\.adapters\.telemetry)\b"
        r"|stream_metrics|webhook_outcome|message_sent|stream_opened|stream_closed",
        re.MULTILINE,
    )
    offenders = [p.name for p in (ROOT / "src" / "api").glob("*.py") if forbidden.search(p.read_text())]
    assert offenders == []


def test_error_handler_records_the_stable_code_for_the_sidecar(client):
    from starlette.requests import Request

    from src.api.errors import _response

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"",
                       "app": app, "state": {}})
    _response(request, 418, "TEAPOT", "short and stout")
    assert request.state.error_code == "TEAPOT"


# ── ordering: a disconnect AFTER the response finished is not the client's doing ──

async def _ended_then_disconnected_app(scope, receive, send):
    """Server finishes an SSE response; the client then closes (as uvicorn reports it)."""
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/event-stream")]})
    await send({"type": "http.response.body", "body": b"retry: 1\n\n", "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})
    await receive()  # the disconnect uvicorn delivers once the client hangs up


async def _client_leaves_midstream_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/event-stream")]})
    await send({"type": "http.response.body", "body": b"retry: 1\n\n", "more_body": True})
    await receive()  # disconnect arrives while the response is still open


async def _drive(app_under_test):
    from src.adapters.http.middleware import TelemetryMiddleware

    async def receive():
        return {"type": "http.disconnect"}  # what the app sees when it next asks

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/x", "raw_path": b"/x", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("t", 80), "client": ("c", 1), "app": app}
    await TelemetryMiddleware(app_under_test)(scope, receive, send)


@pytest.mark.asyncio
async def test_a_stream_the_server_finished_is_server_closed_even_if_the_client_hangs_up_afterwards(m):
    await _drive(_ended_then_disconnected_app)
    assert m.count("stream.connections.closed", transport="sse", reason="server_closed") == 1
    assert m.count("stream.connections.closed", transport="sse", reason="client_disconnect") == 0


@pytest.mark.asyncio
async def test_a_disconnect_while_the_response_is_open_is_the_clients(m):
    await _drive(_client_leaves_midstream_app)
    assert m.count("stream.connections.closed", transport="sse", reason="client_disconnect") == 1
    assert m.count("stream.connections.closed", transport="sse", reason="server_closed") == 0
