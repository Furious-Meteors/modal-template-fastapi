"""Topic scopes and the per-event visibility hook on SSE and WebSocket."""
import asyncio

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from src.api import event_access
from src.api.auth import create_ws_ticket
from src.api.models import TokenPayload
from src.main import app
from tests.asgi_helpers import OpenStream, wait_until
from tests.conftest import TEST_SECRET, make_token

SSE = "/api/v1/events/stream"
WS = "/api/v1/ws"


def _bearer(*scopes):
    return {"Authorization": f"Bearer {make_token(scopes=list(scopes))}"}


def _ticket(*scopes) -> str:
    return create_ws_ticket(TokenPayload(sub="u", exp=9_999_999_999, scopes=list(scopes)))[0]


# ── SSE ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("topic,have,missing", [
    ("items", ["events:read"], "items:read"),
    ("items", ["events:read", "webhooks:read"], "items:read"),
    ("webhooks", ["events:read", "items:read"], "webhooks:read"),
])
def test_sse_needs_the_topic_scope(auth_client, topic, have, missing):
    r = auth_client.get(f"{SSE}?topic={topic}", headers=_bearer(*have))
    assert r.status_code == 403
    assert r.json()["error_code"] == "INSUFFICIENT_SCOPE"
    assert missing in r.json()["detail"]


def test_sse_topic_scope_alone_is_not_enough(auth_client):
    r = auth_client.get(SSE, headers=_bearer("items:read"))  # no events:read
    assert r.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("topic,scope", [("items", "items:read"), ("webhooks", "webhooks:read")])
async def test_sse_allows_events_read_plus_topic_scope(topic, scope):
    stream = OpenStream(app, f"{SSE}?topic={topic}", _bearer("events:read", scope))
    await stream.wait_started()
    assert stream.status == 200
    await stream.disconnect()


@pytest.mark.asyncio
async def test_sse_visibility_hook_filters_per_event(event_bus, monkeypatch):
    monkeypatch.setattr(
        event_access, "can_view",
        lambda event, user: event.data.get("owner") in (None, user.sub),
    )
    stream = OpenStream(app, SSE, _bearer("events:read", "items:read"))  # sub=test@example.com
    await stream.read_until(b"retry")
    await wait_until(lambda: event_bus.subscriber_count("items") == 1)
    await event_bus.publish("items", "e", {"owner": "someone-else", "secret": "HIDDEN"})
    await event_bus.publish("items", "e", {"owner": "test@example.com", "mine": "VISIBLE"})
    body = await stream.read_until(b"VISIBLE")
    assert b"HIDDEN" not in body
    await stream.disconnect()


# ── WebSocket ───────────────────────────────────────────────────────────────

def _close_code(client, url):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url) as ws:
            ws.receive_json()
    return exc.value.code


@pytest.mark.parametrize("topic,scopes", [
    ("items", ["events:read"]),
    ("items", ["events:read", "webhooks:read"]),
    ("webhooks", ["events:read", "items:read"]),
    ("items", ["items:read"]),
])
def test_ws_needs_events_read_and_the_topic_scope(client, topic, scopes):
    assert _close_code(client, f"{WS}?ticket={_ticket(*scopes)}&topic={topic}") == 4403


def test_ws_connects_with_events_read_and_topic_scope(client):
    with client.websocket_connect(
        f"{WS}?ticket={_ticket('events:read', 'webhooks:read')}&topic=webhooks"
    ) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_ticket_carries_only_read_scopes_never_write(auth_client):
    token = make_token(scopes=["events:read", "items:read", "items:write", "webhooks:read", "admin"])
    r = auth_client.post("/api/v1/ws/ticket", headers={"Authorization": f"Bearer {token}"})
    claims = jwt.decode(r.json()["ticket"], TEST_SECRET, algorithms=["HS256"], audience="ws-ticket")
    assert claims["scopes"] == ["events:read", "items:read", "webhooks:read"]


def test_ws_visibility_hook_filters_per_event(client, monkeypatch):
    monkeypatch.setattr(event_access, "can_view", lambda event, user: "HIDDEN" not in event.data.get("name", ""))
    with client.websocket_connect(f"{WS}?ticket={_ticket('events:read', 'items:read')}") as ws:
        client.post("/api/v1/items", json={"name": "HIDDEN item"})
        client.post("/api/v1/items", json={"name": "shown item"})
        assert ws.receive_json()["data"]["name"] == "shown item"


def test_default_hook_allows_everything():
    from src.core.domain.events import Event

    user = TokenPayload(sub="u", exp=9_999_999_999)
    assert event_access.can_view(Event("1", "x", {}, 0.0), user) is True
