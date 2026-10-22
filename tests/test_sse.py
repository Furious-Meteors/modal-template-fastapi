"""Tests for GET /api/v1/events/stream (driven at the ASGI level — see asgi_helpers)."""
import asyncio

import pytest

from src.main import app
from tests.asgi_helpers import OpenStream, wait_until
from tests.conftest import make_token

URL = "/api/v1/events/stream"


def _auth(scopes=("events:read", "items:read", "webhooks:read")):
    return {"Authorization": f"Bearer {make_token(scopes=list(scopes))}"}


def test_requires_auth(auth_client):
    assert auth_client.get(URL).status_code == 401


def test_requires_events_read_scope(auth_client):
    r = auth_client.get(URL, headers={"Authorization": f"Bearer {make_token(scopes=['items:read'])}"})
    assert r.status_code == 403
    assert r.json()["error_code"] == "INSUFFICIENT_SCOPE"


def test_unknown_topic_is_422(auth_client):
    r = auth_client.get(f"{URL}?topic=nope", headers=_auth())
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_stream_headers_and_live_event_arrive_unbuffered(event_bus):
    stream = OpenStream(app, URL, _auth())
    await stream.wait_started()
    assert stream.status == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-cache"
    assert "x-session-id" in stream.headers  # added by the telemetry middleware

    await stream.read_until(b"retry: 3000")
    await wait_until(lambda: event_bus.subscriber_count("items") == 1)
    await event_bus.publish("items", "item.created", {"id": "abc"})
    body = await stream.read_until(b'data: {"id": "abc"}')  # arrives while still open
    assert b"event: item.created" in body
    assert b"id: 1" in body
    assert not stream.task.done()
    await stream.disconnect()


@pytest.mark.asyncio
async def test_last_event_id_replays_missed_events(event_bus):
    first = await event_bus.publish("items", "e", {"n": 1})
    await event_bus.publish("items", "e", {"n": 2})
    stream = OpenStream(app, URL, {**_auth(), "Last-Event-ID": first.id})
    body = await stream.read_until(b'"n": 2')
    assert b'"n": 1' not in body
    await stream.disconnect()


@pytest.mark.asyncio
async def test_topics_are_separate(event_bus):
    stream = OpenStream(app, f"{URL}?topic=webhooks", _auth())
    await stream.read_until(b"retry")
    await wait_until(lambda: event_bus.subscriber_count("webhooks") == 1)
    await event_bus.publish("items", "item.created", {"x": 1})
    await event_bus.publish("webhooks", "webhook.acme", {"y": 2})
    body = await stream.read_until(b'"y": 2')
    assert b'"x": 1' not in body
    await stream.disconnect()


@pytest.mark.asyncio
async def test_heartbeat_when_idle(monkeypatch):
    monkeypatch.setattr("src.api.events.STREAM_HEARTBEAT_SECONDS", 0.05)
    stream = OpenStream(app, URL, _auth())
    await stream.read_until(b": heartbeat")
    await stream.disconnect()


@pytest.mark.asyncio
async def test_stream_ends_itself_at_max_duration(monkeypatch):
    monkeypatch.setattr("src.api.events.STREAM_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr("src.api.events.STREAM_MAX_DURATION_SECONDS", 0.2)
    stream = OpenStream(app, URL, _auth())
    await stream.body_until_end()
    await asyncio.wait_for(stream.task, 2)


@pytest.mark.asyncio
async def test_connection_cap_returns_503_and_slot_freed_on_disconnect(reset_stream_state):
    from src.api import streaming

    streaming.limiter = streaming.StreamLimiter(1)
    first = OpenStream(app, URL, _auth())
    await first.read_until(b"retry")
    assert streaming.limiter.active == 1

    second = OpenStream(app, URL, _auth())
    await second.wait_started()
    assert second.status == 503
    assert second.headers["retry-after"] == "5"
    body = await second.body_until_end()
    assert b"STREAM_LIMIT_REACHED" in body
    await asyncio.wait_for(second.task, 1)
    assert streaming.limiter.active == 1  # the rejected request took no slot

    await first.disconnect()
    assert streaming.limiter.active == 0
    third = OpenStream(app, URL, _auth())
    await third.wait_started()
    assert third.status == 200
    await third.disconnect()


@pytest.mark.asyncio
async def test_item_service_writes_reach_the_stream(fresh_item_service, event_bus):
    stream = OpenStream(app, URL, _auth())
    await stream.read_until(b"retry")
    await wait_until(lambda: event_bus.subscriber_count("items") == 1)
    await fresh_item_service.create("From service")
    body = await stream.read_until(b"item.created")
    assert b"From service" in body
    await stream.disconnect()
