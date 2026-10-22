"""Tests for POST /api/v1/webhooks/{provider} (generic HMAC-SHA256)."""
import json
import time

import pytest

from src.api.webhooks import sign

SECRET = "whsec-test-secret-value"
URL = "/api/v1/webhooks/acme"
BODY = json.dumps({"event": "ping", "n": 1}).encode()


@pytest.fixture(autouse=True)
def provider_secret(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_ACME", SECRET)


def _headers(body: bytes = BODY, *, ts=None, secret=SECRET, webhook_id="evt-1", signature=None, **drop):
    ts = str(int(time.time()) if ts is None else ts)
    headers = {
        "x-webhook-id": webhook_id,
        "x-webhook-timestamp": ts,
        "x-webhook-signature": signature or sign(secret.encode(), ts, body),
        "content-type": "application/json",
    }
    for name in drop:
        headers.pop(name.replace("_", "-"), None)
    return headers


def _post(client, body: bytes = BODY, url: str = URL, **kw):
    return client.post(url, content=body, headers=_headers(body, **kw))


def test_valid_webhook_is_accepted_without_jwt(auth_client):
    r = _post(auth_client)  # auth_client has NO bearer token: HMAC is the auth
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"
    assert r.json()["session_id"] == r.headers["x-session-id"]


def test_publish_payload_shape(client, event_bus):
    _post(client)
    replay = event_bus._history["webhooks"][-1]
    assert replay.type == "webhook.acme"
    assert replay.data == {"webhook_id": "evt-1", "provider": "acme", "payload": {"event": "ping", "n": 1}}


def test_duplicate_delivery_returns_200_and_publishes_once(client, event_bus):
    assert _post(client).status_code == 202
    again = _post(client)
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    assert len(event_bus._history["webhooks"]) == 1


def test_same_id_different_provider_is_not_a_duplicate(client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_OTHER", "another-secret-value")
    assert _post(client).status_code == 202
    ts = str(int(time.time()))
    r = client.post(
        "/api/v1/webhooks/other", content=BODY,
        headers={**_headers(BODY, secret="another-secret-value", ts=ts)},
    )
    assert r.status_code == 202


# ── rejections ──────────────────────────────────────────────────────────────

def test_unknown_provider_404(client):
    r = client.post("/api/v1/webhooks/nobody", content=BODY, headers=_headers())
    assert r.status_code == 404
    assert r.json()["error_code"] == "UNKNOWN_WEBHOOK_PROVIDER"


@pytest.mark.parametrize("name", ["ACME", "a b", "x" * 40, ".hidden"])
def test_malformed_provider_name_404(client, name):
    assert client.post(f"/api/v1/webhooks/{name}", content=BODY, headers=_headers()).status_code == 404


def test_missing_signature_401(client):
    r = _post(client, x_webhook_signature=None)
    assert r.status_code == 401
    assert r.json()["error_code"] == "MISSING_SIGNATURE"


def test_wrong_secret_401(client):
    r = _post(client, secret="not-the-secret")
    assert r.status_code == 401
    assert r.json()["error_code"] == "INVALID_SIGNATURE"


def test_tampered_body_401(client):
    headers = _headers(BODY)
    r = client.post(URL, content=BODY + b" ", headers=headers)
    assert r.status_code == 401


def test_signature_is_bound_to_timestamp(client):
    # Re-using a valid signature with a different timestamp must fail.
    old_ts = int(time.time()) - 10
    sig = sign(SECRET.encode(), str(old_ts), BODY)
    r = _post(client, ts=int(time.time()), signature=sig)
    assert r.status_code == 401


def test_stale_timestamp_400_even_with_valid_signature(client):
    r = _post(client, ts=int(time.time()) - 3600)
    assert r.status_code == 400
    assert r.json()["error_code"] == "STALE_TIMESTAMP"


def test_future_timestamp_400(client):
    assert _post(client, ts=int(time.time()) + 3600).status_code == 400


def test_missing_timestamp_400(client):
    r = client.post(URL, content=BODY, headers=_headers(x_webhook_timestamp=None))
    assert r.status_code == 400
    assert r.json()["error_code"] == "MISSING_TIMESTAMP"


def test_non_numeric_timestamp_400(client):
    r = client.post(URL, content=BODY, headers={**_headers(), "x-webhook-timestamp": "yesterday"})
    assert r.json()["error_code"] == "INVALID_TIMESTAMP"


def test_missing_webhook_id_400(client):
    r = _post(client, x_webhook_id=None)
    assert r.status_code == 400
    assert r.json()["error_code"] == "MISSING_WEBHOOK_ID"


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", b'"str"', b""])
def test_non_object_json_body_400(client, body):
    r = _post(client, body=body)
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_PAYLOAD"


def test_oversized_body_413(client, monkeypatch):
    monkeypatch.setattr("src.api.webhooks.WEBHOOK_MAX_BODY_BYTES", 10)
    r = _post(client)
    assert r.status_code == 413
    assert r.json()["error_code"] == "PAYLOAD_TOO_LARGE"


def test_unauthenticated_calls_do_not_consume_idempotency_ids(client):
    assert _post(client, secret="wrong").status_code == 401
    # The id was never recorded, so the legitimate delivery still goes through.
    assert _post(client).status_code == 202


def test_publish_failure_lets_provider_retry(client, event_bus, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("bus down")

    monkeypatch.setattr(event_bus, "publish", boom)
    from fastapi.testclient import TestClient
    from src.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.post(URL, content=BODY, headers=_headers()).status_code == 500
    monkeypatch.undo()
    monkeypatch.setenv("WEBHOOK_SECRET_ACME", SECRET)
    assert _post(client).status_code == 202  # not treated as a duplicate


# ── the body limit holds for chunked uploads, which carry no Content-Length ──

async def _post_chunked(chunks, *, headers=None, limit=None, monkeypatch=None):
    """Drive the ASGI app with a scripted chunked body; report the status and how many chunks were read."""
    from src.main import app

    consumed = 0
    pending = list(chunks)
    sent = []

    async def receive():
        nonlocal consumed
        if pending:
            consumed += 1
            piece = pending.pop(0)
            return {"type": "http.request", "body": piece, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/v1/webhooks/acme", "raw_path": b"/api/v1/webhooks/acme", "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],  # note: no content-length
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, consumed


@pytest.mark.asyncio
async def test_a_chunked_upload_past_the_limit_is_cut_off_without_reading_the_rest(monkeypatch):
    monkeypatch.setattr("src.api.webhooks.WEBHOOK_MAX_BODY_BYTES", 1000)
    chunks = [b"x" * 400] * 500  # 200 kB offered, 1 kB allowed
    status, consumed = await _post_chunked(chunks, headers=_headers(b"{}"))
    assert status == 413
    assert consumed <= 4, f"read {consumed} chunks of 500: the limit was not enforced while reading"


@pytest.mark.asyncio
async def test_a_chunked_upload_within_the_limit_still_works(monkeypatch):
    body = json.dumps({"event": "ping", "pad": "y" * 3000}).encode()
    pieces = [body[i:i + 700] for i in range(0, len(body), 700)]
    assert len(pieces) > 3
    status, consumed = await _post_chunked(pieces, headers=_headers(body))
    assert status == 202 and consumed == len(pieces)


@pytest.mark.asyncio
async def test_a_chunked_body_exactly_at_the_limit_is_accepted_and_one_byte_over_is_not(monkeypatch):
    base = json.dumps({"p": ""}).encode()
    limit = 600
    monkeypatch.setattr("src.api.webhooks.WEBHOOK_MAX_BODY_BYTES", limit)
    exact = json.dumps({"p": "z" * (limit - len(base))}).encode()
    assert len(exact) == limit
    assert (await _post_chunked([exact[:300], exact[300:]], headers=_headers(exact)))[0] == 202
    over = exact + b" "
    assert (await _post_chunked([over[:300], over[300:]], headers=_headers(over)))[0] == 413
