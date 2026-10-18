"""Tests for the ticket exchange and the /api/v1/ws WebSocket stream."""
import time

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from src.api.auth import create_ws_ticket
from src.api.models import TokenPayload
from tests.conftest import TEST_SECRET, make_token

WS = "/api/v1/ws"
USER = TokenPayload(sub="u@example.com", exp=9_999_999_999, scopes=["events:read"])


def _ticket() -> str:
    return create_ws_ticket(USER)[0]


def _expect_close(client, url: str) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url) as ws:
            ws.receive_json()
    return exc.value.code


# ── ticket issuance ─────────────────────────────────────────────────────────

def test_ticket_endpoint_requires_auth(auth_client):
    assert auth_client.post("/api/v1/ws/ticket").status_code == 401


def test_ticket_endpoint_requires_events_read(auth_client):
    token = make_token(scopes=["items:read"])
    r = auth_client.post("/api/v1/ws/ticket", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_ticket_endpoint_returns_short_lived_scoped_ticket(auth_client):
    token = make_token(scopes=["events:read", "items:write"])
    r = auth_client.post("/api/v1/ws/ticket", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["expires_in"] == 30
    claims = jwt.decode(body["ticket"], TEST_SECRET, algorithms=["HS256"], audience="ws-ticket")
    assert claims["scopes"] == ["events:read"]  # least privilege: write scope not copied
    assert claims["exp"] - claims["iat"] == 30


def test_ticket_cannot_be_used_as_api_token(auth_client):
    r = auth_client.get("/api/v1/items", headers={"Authorization": f"Bearer {_ticket()}"})
    assert r.status_code == 401


def test_api_token_cannot_be_used_as_ticket(client):
    code = _expect_close(client, f"{WS}?ticket={make_token(scopes=['events:read'])}")
    assert code == 4401


# ── connection ──────────────────────────────────────────────────────────────

def test_missing_ticket_closes_4401(client):
    assert _expect_close(client, WS) == 4401


def test_garbage_ticket_closes_4401(client):
    assert _expect_close(client, f"{WS}?ticket=garbage") == 4401


def test_expired_ticket_closes_4401(client):
    expired = jwt.encode(
        {"sub": "u", "aud": "ws-ticket", "scopes": ["events:read"], "jti": "x",
         "iat": int(time.time()) - 100, "exp": int(time.time()) - 10},
        TEST_SECRET, algorithm="HS256",
    )
    assert _expect_close(client, f"{WS}?ticket={expired}") == 4401


def test_ticket_without_scope_closes_4403(client):
    ticket = jwt.encode(
        {"sub": "u", "aud": "ws-ticket", "scopes": [], "jti": "y",
         "iat": int(time.time()), "exp": int(time.time()) + 30},
        TEST_SECRET, algorithm="HS256",
    )
    assert _expect_close(client, f"{WS}?ticket={ticket}") == 4403


def test_ticket_is_single_use(client, monkeypatch):
    monkeypatch.setattr("src.api.events.STREAM_HEARTBEAT_SECONDS", 0.05)
    ticket = _ticket()
    with client.websocket_connect(f"{WS}?ticket={ticket}") as ws:
        assert ws.receive_json() == {"type": "heartbeat"}
    assert _expect_close(client, f"{WS}?ticket={ticket}") == 4401


def test_unknown_topic_closes_4400(client):
    assert _expect_close(client, f"{WS}?ticket={_ticket()}&topic=nope") == 4400


def test_receives_item_events(client):
    with client.websocket_connect(f"{WS}?ticket={_ticket()}") as ws:
        client.post("/api/v1/items", json={"name": "ws-item"})
        msg = ws.receive_json()
    assert msg["type"] == "event"
    assert msg["event"] == "item.created"
    assert msg["data"]["name"] == "ws-item"
    assert msg["id"]


def test_replay_with_last_event_id(client):
    client.post("/api/v1/items", json={"name": "one"})
    client.post("/api/v1/items", json={"name": "two"})
    with client.websocket_connect(f"{WS}?ticket={_ticket()}&last_event_id=1") as ws:
        assert ws.receive_json()["data"]["name"] == "two"


def test_ping_pong_and_bad_messages(client):
    with client.websocket_connect(f"{WS}?ticket={_ticket()}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_text("not json")
        assert ws.receive_json() == {"type": "error", "code": "INVALID_JSON"}
        ws.send_json({"type": "subscribe"})
        assert ws.receive_json() == {"type": "error", "code": "UNSUPPORTED_MESSAGE"}


def test_oversized_message_closes_1009(client, monkeypatch):
    monkeypatch.setattr("src.api.events.WS_MAX_MESSAGE_BYTES", 16)
    with client.websocket_connect(f"{WS}?ticket={_ticket()}") as ws:
        ws.send_text("x" * 100)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1009


def test_max_duration_closes_1001(client, monkeypatch):
    monkeypatch.setattr("src.api.events.STREAM_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr("src.api.events.STREAM_MAX_DURATION_SECONDS", 0.2)
    with client.websocket_connect(f"{WS}?ticket={_ticket()}") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            while True:
                ws.receive_json()
    assert exc.value.code == 1001


def test_connection_cap_closes_1013_and_slot_is_released(client, reset_stream_state):
    from src.api import streaming

    streaming.limiter = streaming.StreamLimiter(1)
    with client.websocket_connect(f"{WS}?ticket={_ticket()}") as first:
        assert streaming.limiter.active == 1
        assert _expect_close(client, f"{WS}?ticket={_ticket()}") == 1013
        first.send_json({"type": "ping"})
        assert first.receive_json() == {"type": "pong"}
    # TestClient exits the socket; the server task releases asynchronously.
    deadline = time.time() + 2
    while streaming.limiter.active and time.time() < deadline:
        time.sleep(0.02)
    assert streaming.limiter.active == 0
