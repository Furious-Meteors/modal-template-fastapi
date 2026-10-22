"""Rate limiting: the token bucket, and where it is wired in."""
import json
import time

import pytest

from src.api import ratelimit
from src.api.ratelimit import TokenBucketLimiter
from src.api.webhooks import sign
from tests.conftest import make_token


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ── the bucket ──────────────────────────────────────────────────────────────

def test_burst_then_blocked_with_a_retry_hint():
    clock = Clock()
    lim = TokenBucketLimiter(rate_per_second=1, burst=3, clock=clock)
    assert [lim.check("k") for _ in range(3)] == [0, 0, 0]
    wait = lim.check("k")
    assert wait == pytest.approx(1.0)


def test_tokens_refill_over_time_up_to_the_burst():
    clock = Clock()
    lim = TokenBucketLimiter(rate_per_second=2, burst=4, clock=clock)
    for _ in range(4):
        lim.check("k")
    assert lim.check("k") > 0
    clock.now += 0.5  # one token back
    assert lim.check("k") == 0
    assert lim.check("k") > 0
    clock.now += 1000  # long idle never exceeds the burst
    assert [lim.check("k") for _ in range(4)] == [0, 0, 0, 0]
    assert lim.check("k") > 0


def test_keys_are_independent():
    lim = TokenBucketLimiter(rate_per_second=1, burst=1, clock=Clock())
    assert lim.check("a") == 0
    assert lim.check("a") > 0
    assert lim.check("b") == 0  # b is unaffected by a


def test_memory_is_bounded_by_evicting_the_least_recently_used_key():
    lim = TokenBucketLimiter(rate_per_second=1, burst=1, max_keys=3, clock=Clock())
    for i in range(10):
        lim.check(f"k{i}")
    assert len(lim._buckets) == 3
    assert list(lim._buckets) == ["k7", "k8", "k9"]


def test_retry_after_header_is_whole_seconds_and_at_least_one():
    assert TokenBucketLimiter.retry_after_header(0.2) == {"Retry-After": "1"}
    assert TokenBucketLimiter.retry_after_header(2.1) == {"Retry-After": "3"}


# ── ticket endpoint: per user ───────────────────────────────────────────────

def test_ticket_endpoint_is_limited_per_user(auth_client):
    ratelimit.ticket_limiter = TokenBucketLimiter(rate_per_second=0.001, burst=3)
    alice = {"Authorization": f"Bearer {make_token(sub='alice', scopes=['events:read'])}"}
    bob = {"Authorization": f"Bearer {make_token(sub='bob', scopes=['events:read'])}"}
    assert [auth_client.post("/api/v1/ws/ticket", headers=alice).status_code for _ in range(3)] == [200] * 3
    blocked = auth_client.post("/api/v1/ws/ticket", headers=alice)
    assert blocked.status_code == 429
    assert blocked.json()["error_code"] == "RATE_LIMITED"
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked.json()["session_id"] == blocked.headers["x-session-id"]
    assert auth_client.post("/api/v1/ws/ticket", headers=bob).status_code == 200  # not shared


# ── webhooks: per provider, generous ────────────────────────────────────────

SECRET = "whsec-test-secret-value"
BODY = json.dumps({"a": 1}).encode()


def _hook(client, provider="acme", wid="1"):
    ts = str(int(time.time()))
    return client.post(
        f"/api/v1/webhooks/{provider}", content=BODY,
        headers={"x-webhook-id": wid, "x-webhook-timestamp": ts,
                 "x-webhook-signature": sign(SECRET.encode(), ts, BODY)},
    )


def test_webhook_limit_is_per_provider_and_returns_429(client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_ACME", SECRET)
    monkeypatch.setenv("WEBHOOK_SECRET_OTHER", SECRET)
    ratelimit.webhook_limiter = TokenBucketLimiter(rate_per_second=0.001, burst=2)
    assert _hook(client, wid="1").status_code == 202
    assert _hook(client, wid="2").status_code == 202
    limited = _hook(client, wid="3")
    assert limited.status_code == 429
    assert limited.json()["error_code"] == "RATE_LIMITED"
    assert "retry-after" in limited.headers
    assert _hook(client, provider="other", wid="1").status_code == 202  # other provider unaffected


def test_unknown_provider_404s_before_consuming_the_limit(client, monkeypatch):
    ratelimit.webhook_limiter = TokenBucketLimiter(rate_per_second=0.001, burst=1)
    for _ in range(5):
        assert client.post("/api/v1/webhooks/nobody", content=BODY).status_code == 404
    assert ratelimit.webhook_limiter.check("nobody") == 0  # bucket never touched


def test_bad_signatures_do_not_lock_out_a_legitimate_sender_sharing_an_ip(client, monkeypatch):
    """Keying by provider, not IP, means attackers behind a shared proxy IP cannot lock out real traffic."""
    monkeypatch.setenv("WEBHOOK_SECRET_ACME", SECRET)
    for _ in range(30):  # a burst of forged requests from the "same IP"
        client.post("/api/v1/webhooks/acme", content=BODY,
                    headers={"x-webhook-timestamp": str(int(time.time())), "x-webhook-signature": "sha256=bad"})
    assert _hook(client, wid="legit").status_code == 202


# ── websocket messages: per connection ──────────────────────────────────────

def test_ws_message_flood_closes_with_1008(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from src.api.auth import create_ws_ticket
    from src.api.models import TokenPayload

    monkeypatch.setattr(ratelimit, "new_ws_message_limiter", lambda: TokenBucketLimiter(0.001, burst=3))
    ticket = create_ws_ticket(TokenPayload(sub="u", exp=9_999_999_999, scopes=["events:read", "items:read"]))[0]
    with client.websocket_connect(f"/api/v1/ws?ticket={ticket}") as ws:
        for _ in range(3):
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
        ws.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1008


def test_ws_limit_is_per_connection(client, monkeypatch):
    from src.api.auth import create_ws_ticket
    from src.api.models import TokenPayload

    monkeypatch.setattr(ratelimit, "new_ws_message_limiter", lambda: TokenBucketLimiter(0.001, burst=2))
    for _ in range(2):  # two separate connections each get a fresh allowance
        ticket = create_ws_ticket(TokenPayload(sub="u", exp=9_999_999_999, scopes=["events:read", "items:read"]))[0]
        with client.websocket_connect(f"/api/v1/ws?ticket={ticket}") as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
