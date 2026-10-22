#!/usr/bin/env python
"""
Smoke-test a running deployment end to end: auth, CRUD, uniqueness under concurrency,
scopes, Server-Sent Events, WebSockets and signed webhooks.

    python scripts/smoke_test.py --url https://<your-app>.modal.run --jwt-secret "$JWT_SECRET" --yes
    python scripts/smoke_test.py --url https://<your-app>.modal.run --token "$TOKEN" --yes

It WRITES data (items named smoke-<timestamp>-*, plus the events and webhook ids they cause);
items are deleted afterwards, events expire with the bus's retention. Pass --yes to confirm
(not needed for localhost). Do not point it at an environment whose data you cannot afford to
touch.

Credentials (flags, or the env vars in brackets):
  --token / --limited-token  [SMOKE_TOKEN, SMOKE_LIMITED_TOKEN]  tokens with items:read items:write
                             events:read webhooks:read, and (limited) items:read events:read only
  --jwt-secret               [SMOKE_JWT_SECRET, JWT_SECRET]      mint both tokens locally instead
  --webhook-secret           [SMOKE_WEBHOOK_SECRET]              enables the signed-webhook checks
  --provider                 default "acme"                      which WEBHOOK_SECRET_<PROVIDER> it is

Modal deployments keep the JWT secret unreadable: scripts/modal_mint_token.py mints short-lived
tokens inside Modal. Per-container behaviour (the stream cap, rate limits) is deliberately NOT
checked here: with several containers a smoke test cannot prove it either way.

Exit code: 0 all checks passed (skips are fine), 1 a check failed, 2 bad usage.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from typing import Optional
from urllib.parse import urlsplit

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
FULL_SCOPES = ["events:read", "items:read", "items:write", "webhooks:read"]
LIMITED_SCOPES = ["events:read", "items:read"]


class Smoke:
    def __init__(self, base: str, token: str, limited: Optional[str], webhook_secret: Optional[str],
                 provider: str, concurrency: int, only: Optional[set[str]], skip: set[str]) -> None:
        import httpx

        self.httpx = httpx
        self.base = base.rstrip("/")
        self.api = self.base + "/api/v1"
        self.ws_url = self.base.replace("https://", "wss://").replace("http://", "ws://") + "/api/v1/ws"
        self.H = {"Authorization": f"Bearer {token}"}
        self.HL = {"Authorization": f"Bearer {limited}"} if limited else None
        self.webhook_secret = webhook_secret.encode() if webhook_secret else None
        self.provider, self.concurrency = provider, concurrency
        self.only, self.skip_groups = only, skip
        self.tag = f"smoke-{int(time.time())}"
        self.passed = self.failed = self.skipped = 0
        self.created: list[str] = []

    # ── reporting ───────────────────────────────────────────────────────
    def ok(self, name: str, cond: bool, detail: str = "") -> bool:
        self.passed += bool(cond)
        self.failed += not cond
        print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
        return bool(cond)

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"SKIP {name}  [{why}]", flush=True)

    def wanted(self, group: str) -> bool:
        return (self.only is None or group in self.only) and group not in self.skip_groups

    # ── helpers ─────────────────────────────────────────────────────────
    async def open_stream(self, c, topic: str, headers: dict, tries: int = 4):
        """Open an SSE stream, honouring Retry-After on 503 (a closed stream can hold its slot briefly)."""
        for _ in range(tries):
            ctx = c.stream("GET", f"{self.api}/events/stream?topic={topic}", headers=headers, timeout=60)
            r = await ctx.__aenter__()
            if r.status_code != 503:
                return ctx, r
            await ctx.__aexit__(None, None, None)
            await asyncio.sleep(int(r.headers.get("retry-after", "5")) + 0.5)
        return None, None

    async def run(self) -> int:
        async with self.httpx.AsyncClient(timeout=60, limits=self.httpx.Limits(max_connections=200)) as c:
            try:
                if self.wanted("basic"): await self.basic(c)
                if self.wanted("scopes"): await self.scopes(c)
                if self.wanted("race"): await self.race(c)
                if self.wanted("sse"): await self.sse(c)
                if self.wanted("ws"): await self.websocket(c)
                if self.wanted("webhooks"): await self.webhooks(c)
            finally:
                for item_id in self.created:  # best-effort cleanup, even after a failure
                    try: await c.delete(f"{self.api}/items/{item_id}", headers=self.H)
                    except Exception: pass
        print(f"\n{self.passed} passed, {self.failed} failed, {self.skipped} skipped  (tag {self.tag})")
        return 1 if self.failed else 0

    # ── checks ──────────────────────────────────────────────────────────
    async def basic(self, c) -> None:
        r = await c.get(f"{self.api}/health")
        self.ok("health is healthy (reads the database)", r.status_code == 200 and r.json().get("status") == "healthy")
        r = await c.get(f"{self.api}/items")
        self.ok("no token -> 401 MISSING_TOKEN", r.status_code == 401 and r.json().get("error_code") == "MISSING_TOKEN")
        r = await c.get(f"{self.api}/items", headers={"Authorization": "Bearer garbage"})
        self.ok("bad token -> 401 INVALID_TOKEN", r.status_code == 401 and r.json().get("error_code") == "INVALID_TOKEN")

        t = time.perf_counter()
        r = await c.post(f"{self.api}/items", json={"name": f"{self.tag}-a", "description": "d"}, headers=self.H)
        created = r.status_code == 201
        self.ok("create item -> 201", created, f"{(time.perf_counter() - t) * 1000:.0f}ms")
        if not created:
            return
        item_id = r.json()["data"]["id"]
        self.created.append(item_id)
        r = await c.get(f"{self.api}/items/{item_id}", headers=self.H)
        self.ok("get item", r.status_code == 200 and r.json()["data"]["name"] == f"{self.tag}-a")
        r = await c.put(f"{self.api}/items/{item_id}", json={"name": f"{self.tag}-a2"}, headers=self.H)
        self.ok("update item (full replace)", r.status_code == 200 and r.json()["data"]["description"] is None)
        r = await c.get(f"{self.api}/items?limit=2", headers=self.H)
        self.ok("list is paginated", r.status_code == 200 and {"items", "total", "limit", "offset", "has_more"} <= set(r.json()))
        r = await c.get(f"{self.api}/items/does-not-exist", headers=self.H)
        self.ok("missing item -> 404 ITEM_NOT_FOUND", r.status_code == 404 and r.json().get("error_code") == "ITEM_NOT_FOUND")
        r = await c.post(f"{self.api}/items", json={"name": "   "}, headers=self.H)
        self.ok("blank name -> 422 with field errors", r.status_code == 422 and bool(r.json().get("errors")))
        r = await c.post(f"{self.api}/items", json={"name": f"  {self.tag}-A2 "}, headers=self.H)
        self.ok("duplicate name (case/space) -> 409", r.status_code == 409 and r.json().get("error_code") == "ITEM_NAME_EXISTS")
        r = await c.delete(f"{self.api}/items/{item_id}", headers=self.H)
        self.ok("delete -> 204", r.status_code == 204)
        self.created.remove(item_id)
        r = await c.delete(f"{self.api}/items/{item_id}", headers=self.H)
        self.ok("delete again -> 404", r.status_code == 404)

    async def scopes(self, c) -> None:
        if not self.HL:
            return self.skip("scope checks", "no --limited-token / --jwt-secret")
        r = await c.post(f"{self.api}/items", json={"name": f"{self.tag}-ro"}, headers=self.HL)
        self.ok("read-only token cannot write -> 403", r.status_code == 403 and r.json().get("error_code") == "INSUFFICIENT_SCOPE")
        r = await c.get(f"{self.api}/events/stream?topic=webhooks", headers=self.HL)
        self.ok("webhooks stream needs webhooks:read -> 403", r.status_code == 403)

    async def race(self, c) -> None:
        n = self.concurrency
        rs = await asyncio.gather(*(c.post(f"{self.api}/items", json={"name": f"{self.tag}-race"}, headers=self.H) for _ in range(n)))
        codes = sorted(r.status_code for r in rs)
        for r in rs:
            if r.status_code == 201:
                self.created.append(r.json()["data"]["id"])
        self.ok(f"{n} concurrent creates of one name: exactly one 201", codes.count(201) == 1 and codes.count(409) == n - 1,
                f"201x{codes.count(201)} 409x{codes.count(409)} other={[x for x in codes if x not in (201, 409)]}")

    async def sse(self, c) -> None:
        got: dict = {}
        ctx, r = await self.open_stream(c, "items", self.H)
        if r is None:
            return self.ok("SSE stream opens", False, "still 503 after retries")
        try:
            self.ok("SSE: 200 text/event-stream", r.status_code == 200 and r.headers.get("content-type", "").startswith("text/event-stream"))

            async def read():
                async for line in r.aiter_lines():
                    if line.startswith("retry:"): got.setdefault("first", time.perf_counter())
                    if line.startswith("data:") and f"{self.tag}-sse" in line:
                        got["event"] = time.perf_counter(); return
            reader = asyncio.create_task(read())
            await asyncio.sleep(2)  # let the subscription settle
            t0 = time.perf_counter()
            made = await c.post(f"{self.api}/items", json={"name": f"{self.tag}-sse"}, headers=self.H)
            if made.status_code == 201: self.created.append(made.json()["data"]["id"])
            try:
                await asyncio.wait_for(reader, 30)
            except asyncio.TimeoutError:
                reader.cancel()
            self.ok("SSE: an item created by another request arrives on the stream", "event" in got,
                    f"{(got['event'] - t0) * 1000:.0f}ms after the POST began" if "event" in got else "no event within 30s")
        finally:
            await ctx.__aexit__(None, None, None)

    async def websocket(self, c) -> None:
        import websockets

        tr = await c.post(f"{self.api}/ws/ticket", headers=self.H)
        if not self.ok("ws ticket issued", tr.status_code == 200 and tr.json().get("expires_in", 0) > 0):
            return
        ticket = tr.json()["ticket"]
        try:
            async with websockets.connect(f"{self.ws_url}?ticket={ticket}", open_timeout=30) as ws:
                await ws.send(json.dumps({"type": "ping"}))
                self.ok("ws ping/pong", json.loads(await asyncio.wait_for(ws.recv(), 20)) == {"type": "pong"})
                await asyncio.sleep(2)
                made = await c.post(f"{self.api}/items", json={"name": f"{self.tag}-ws"}, headers=self.H)
                if made.status_code == 201: self.created.append(made.json()["data"]["id"])
                event = None
                for _ in range(8):  # skip heartbeats
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
                    if msg.get("type") == "event": event = msg; break
                self.ok("ws receives the item event", bool(event) and event["data"].get("name") == f"{self.tag}-ws")
        except Exception as exc:
            self.ok("ws session", False, type(exc).__name__)
        for name, url in (("ws ticket cannot be replayed (4401)", f"{self.ws_url}?ticket={ticket}"), ("ws without a ticket is refused (4401)", self.ws_url)):
            try:
                async with websockets.connect(url, open_timeout=30) as ws:
                    await asyncio.wait_for(ws.recv(), 10)
                self.ok(name, False, "connection stayed open")
            except websockets.ConnectionClosed as e:
                self.ok(name, e.rcvd is not None and e.rcvd.code == 4401, f"code={e.rcvd.code if e.rcvd else None}")
            except Exception as exc:
                self.ok(name, False, type(exc).__name__)

    async def webhooks(self, c) -> None:
        r = await c.post(f"{self.api}/webhooks/nobody-{self.tag}", content=b"{}")
        self.ok("webhook for an unknown provider -> 404", r.status_code == 404 and r.json().get("error_code") == "UNKNOWN_WEBHOOK_PROVIDER")
        if not self.webhook_secret:
            return self.skip("signed webhook checks", "no --webhook-secret")
        body = json.dumps({"smoke": self.tag}).encode()

        def send(wid: str, secret: bytes, ts: Optional[int] = None):
            stamp = str(int(time.time()) if ts is None else ts)
            sig = "sha256=" + hmac.new(secret, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
            return c.post(f"{self.api}/webhooks/{self.provider}", content=body,
                          headers={"x-webhook-id": wid, "x-webhook-timestamp": stamp, "x-webhook-signature": sig})

        r1 = await send(f"{self.tag}-w1", self.webhook_secret)
        self.ok("signed webhook accepted -> 202", r1.status_code == 202 and r1.json().get("status") == "accepted")
        r2 = await send(f"{self.tag}-w1", self.webhook_secret)
        self.ok("same delivery id again -> 200 duplicate", r2.status_code == 200 and r2.json().get("status") == "duplicate")
        self.ok("wrong signature -> 401", (await send(f"{self.tag}-w2", b"wrong-secret")).status_code == 401)
        self.ok("stale timestamp -> 400", (await send(f"{self.tag}-w3", self.webhook_secret, int(time.time()) - 3600)).status_code == 400)
        rs = await asyncio.gather(*(send(f"{self.tag}-dup", self.webhook_secret) for _ in range(20)))
        codes = sorted(r.status_code for r in rs)
        self.ok("20 simultaneous deliveries of one id: exactly one accepted", codes.count(202) == 1 and codes.count(200) == 19, f"202x{codes.count(202)} 200x{codes.count(200)}")


def _mint(secret: str, scopes: list[str]) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode({"sub": "smoke-test", "iat": now, "exp": now + 1800, "scopes": scopes}, secret, algorithm="HS256")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("SMOKE_URL"))
    p.add_argument("--token", default=os.environ.get("SMOKE_TOKEN"))
    p.add_argument("--limited-token", default=os.environ.get("SMOKE_LIMITED_TOKEN"))
    p.add_argument("--jwt-secret", default=os.environ.get("SMOKE_JWT_SECRET") or os.environ.get("JWT_SECRET"))
    p.add_argument("--webhook-secret", default=os.environ.get("SMOKE_WEBHOOK_SECRET"))
    p.add_argument("--provider", default="acme")
    p.add_argument("--concurrency", type=int, default=30, help="simultaneous creates in the uniqueness race")
    p.add_argument("--only", help="comma-separated groups: basic,scopes,race,sse,ws,webhooks")
    p.add_argument("--skip", default="", help="comma-separated groups to skip")
    p.add_argument("--yes", action="store_true", help="confirm that writing test data to this deployment is OK")
    a = p.parse_args(argv)

    if not a.url:
        print("--url is required (or SMOKE_URL).", file=sys.stderr)
        return 2
    host = urlsplit(a.url).hostname or ""
    if host not in LOCAL_HOSTS and not a.yes:
        print(f"This test WRITES data to {host}. Re-run with --yes to confirm.", file=sys.stderr)
        return 2
    token, limited = a.token, a.limited_token
    if not token and a.jwt_secret:
        token, limited = _mint(a.jwt_secret, FULL_SCOPES), limited or _mint(a.jwt_secret, LIMITED_SCOPES)
    if not token:
        print("Give --token, or --jwt-secret to mint one (Modal: scripts/modal_mint_token.py).", file=sys.stderr)
        return 2
    smoke = Smoke(a.url, token, limited, a.webhook_secret, a.provider, max(2, a.concurrency),
                  set(a.only.split(",")) if a.only else None, {s for s in a.skip.split(",") if s})
    return asyncio.run(smoke.run())


if __name__ == "__main__":
    raise SystemExit(main())
