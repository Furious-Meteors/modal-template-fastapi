# Streaming & Webhooks

Three ways to move events across the API boundary. All three sit on one
`EventBus` port, so swapping the backend never touches a route.

| | Direction | Auth | Use for |
|---|---|---|---|
| [Server-Sent Events](#server-sent-events) | server → client | Bearer JWT, `events:read` | Dashboards, live feeds; browsers reconnect for free |
| [WebSocket](#websocket) | both | short-lived ticket | Interactive clients that also send messages |
| [Webhooks](#webhooks) | third party → server | HMAC signature | Receiving events from other systems |

!!! warning "Single-container by default"
    The shipped `EventBus` and webhook store are **in-memory**. Under Modal
    autoscaling a stream on container A will not see events published on
    container B, and duplicate webhooks that land on different containers are
    not detected. Production logs a warning at startup. Add a shared adapter
    (Modal `Queue`/`Dict`, Redis, …) before scaling out — see
    [Adding an event-bus backend](#adding-an-event-bus-backend).

## Topics and events

Streams subscribe to a **topic**: `items` (default) or `webhooks`.

| Topic | Event types | `data` |
|---|---|---|
| `items` | `item.created`, `item.updated` | the item |
| `items` | `item.deleted` | `{"id": "..."}` |
| `webhooks` | `webhook.<provider>` | `{"webhook_id", "provider", "payload"}` |

Every event has a monotonically increasing `id`, used for resume.

## Server-Sent Events

`GET /api/v1/events/stream?topic=items` · **JWT required** · scope `events:read`

```bash
curl -N -H "Authorization: Bearer $TOKEN" "$BASE_URL/events/stream"
```

```text
retry: 3000

id: 7
event: item.created
data: {"id": "…", "name": "Widget", "description": null, "project_id": null}

: heartbeat
```

* `: heartbeat` comments arrive every `STREAM_HEARTBEAT_SECONDS` while idle.
* Send `Last-Event-ID: <id>` to resume; missed events still in the bus's replay
  buffer (last 100 per topic) are replayed first. Replay is best-effort.
* The server ends the stream after `STREAM_MAX_DURATION_SECONDS` so it stays
  inside Modal's request timeout. `EventSource` reconnects automatically and
  sends `Last-Event-ID`.
* A client too slow to keep up is disconnected rather than buffered without
  bound; it reconnects and resumes.

```js
const es = new EventSource(url); // browsers cannot set headers — see note
```

!!! note "Browsers and Authorization headers"
    `EventSource` cannot send an `Authorization` header. Use `fetch()` with a
    streaming body reader (or a library such as `@microsoft/fetch-event-source`)
    to send the Bearer token.

## WebSocket

Browsers cannot set headers on `new WebSocket(...)`, so authentication is a
two-step exchange:

```bash
# 1. Trade your JWT for a ticket (valid WS_TICKET_TTL_SECONDS = 30s, single use)
curl -s -X POST -H "Authorization: Bearer $TOKEN" "$BASE_URL/ws/ticket"
# {"ticket": "eyJ…", "expires_in": 30}

# 2. Connect
wscat -c "wss://<host>/api/v1/ws?ticket=eyJ…&topic=items"
```

Messages are JSON:

| Direction | Message |
|---|---|
| server → client | `{"type":"event","id":"7","event":"item.created","data":{…}}` |
| server → client | `{"type":"heartbeat"}` while idle |
| client → server | `{"type":"ping"}` → `{"type":"pong"}` |
| server → client | `{"type":"error","code":"INVALID_JSON" \| "UNSUPPORTED_MESSAGE"}` |

Optional query parameters: `topic`, `last_event_id` (resume, as for SSE).

**Close codes**

| Code | Meaning | Client action |
|---|---|---|
| `1001` | max duration reached | reconnect (get a new ticket) |
| `1009` | message larger than `WS_MAX_MESSAGE_BYTES` | fix the client |
| `1013` | server busy or slow consumer | back off, retry |
| `4400` | unknown topic | fix the request |
| `4401` | missing, invalid, expired or already-used ticket | get a new ticket |
| `4403` | ticket lacks `events:read` | — |

**Ticket design.** A ticket is a signed JWT with its own audience, so it cannot
be used as an API token (and API tokens cannot be used as tickets). It carries
only `events:read`. It is stateless, so any container can verify it; single-use
is enforced per container on a best-effort basis, with a 30 s lifetime as the
real bound.

!!! warning "Tickets appear in URLs"
    The ticket is in the query string, so it can show up in access logs (uvicorn
    logs it). That is why it is short-lived and single-use. Do not log or store
    full WebSocket URLs downstream without scrubbing `ticket=`.

## Webhooks

`POST /api/v1/webhooks/{provider}` · **no JWT** — authenticated by HMAC.

| Header | Value |
|---|---|
| `X-Webhook-Id` | unique delivery id (idempotency key), 1–128 printable chars |
| `X-Webhook-Timestamp` | unix seconds |
| `X-Webhook-Signature` | `sha256=` + hex HMAC-SHA256 of `"<timestamp>.<raw body>"` |

The secret for provider `acme` is the env var `WEBHOOK_SECRET_ACME` — put it in
a Modal secret. Provider names are `[a-z0-9][a-z0-9_-]{0,31}`; hyphens become
underscores in the variable name.

```python
import hashlib, hmac, json, time, requests

body = json.dumps({"event": "invoice.paid"}).encode()
ts = str(int(time.time()))
sig = "sha256=" + hmac.new(SECRET, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
requests.post(f"{BASE_URL}/webhooks/acme", data=body, headers={
    "X-Webhook-Id": "evt-123", "X-Webhook-Timestamp": ts, "X-Webhook-Signature": sig,
    "Content-Type": "application/json",
})
```

Checks run in this order, and nothing is parsed or stored until the signature
verifies:

| Check | Failure |
|---|---|
| provider configured | `404 UNKNOWN_WEBHOOK_PROVIDER` |
| body ≤ `WEBHOOK_MAX_BODY_BYTES` (1 MiB) | `413 PAYLOAD_TOO_LARGE` |
| timestamp present and numeric | `400 MISSING_TIMESTAMP` / `INVALID_TIMESTAMP` |
| timestamp within ±`WEBHOOK_TOLERANCE_SECONDS` (300) | `400 STALE_TIMESTAMP` |
| signature present, constant-time match | `401 MISSING_SIGNATURE` / `INVALID_SIGNATURE` |
| `X-Webhook-Id` present | `400 MISSING_WEBHOOK_ID` |
| body is a JSON object | `400 INVALID_PAYLOAD` |

Responses: `202 {"status":"accepted"}` on first delivery; `200
{"status":"duplicate"}` for a repeat of the same `(provider, X-Webhook-Id)`. If
publishing fails the id is forgotten and a 500 is returned, so the provider's
retry is processed rather than dropped as a duplicate.

The handler only validates and publishes to the `webhooks` topic and returns
immediately. Do the real work in a consumer of that topic.

## Capacity: the connection cap

Each open stream holds one of Modal's `max_concurrent_requests` slots for its
whole life. To keep normal requests from being starved:

* `EnvConfig.max_streams` (default 3) caps concurrent SSE + WebSocket
  connections per container, exported as `STREAM_MAX_CONNECTIONS`.
* `EnvConfig` refuses to load unless `max_streams < max_concurrent_requests`.
* Over the cap: SSE returns `503 STREAM_LIMIT_REACHED` with `Retry-After: 5`;
  WebSocket closes with `1013`.
* `STREAM_MAX_DURATION_SECONDS` is derived as
  `server_hard_timeout_seconds - stream_timeout_margin_seconds` (150 − 30 = 120).

If you need many long-lived connections, raise `max_concurrent_requests` and
`max_streams` together, or move streaming to its own Modal function.

## Configuration

| Variable | Default | Set by |
|---|---|---|
| `EVENT_BUS_BACKEND` | `memory` | Modal secret / env |
| `STREAM_MAX_CONNECTIONS` | 50 locally, `max_streams` on Modal | `modal_common.py` |
| `STREAM_MAX_DURATION_SECONDS` | 120 on Modal | `modal_common.py` |
| `STREAM_HEARTBEAT_SECONDS` | 15 | env |
| `WS_MAX_MESSAGE_BYTES` | 4096 | env |
| `WS_TICKET_TTL_SECONDS` | 30 | env |
| `WEBHOOK_TOLERANCE_SECONDS` | 300 | env |
| `WEBHOOK_MAX_BODY_BYTES` | 1048576 | env |
| `WEBHOOK_SECRET_<PROVIDER>` | — (required per provider) | Modal secret |

Invalid values (non-integer, < 1) stop the app at startup.

## Observability

* One span per HTTP request and **one span per WebSocket connection**
  (`WEBSOCKET /api/v1/ws`, with `websocket.close_code`). There is deliberately no
  per-message span.
* `stream.connections.active` (gauge, by `transport`), `stream.connections.closed`
  (by `transport`, `reason`: `client_disconnect`, `max_duration`, `slow_consumer`,
  `message_too_large`), `stream.messages.sent`, `webhook.received` (by
  `provider`, `outcome`: `accepted`, `duplicate`, `bad_signature`, `stale`,
  `bad_timestamp`, `bad_request`, `too_large`).
* `TelemetryMiddleware` is a pure ASGI middleware so streaming bodies pass
  through unbuffered and client disconnects reach the endpoint.

## Adding an event-bus backend

1. Create `src/adapters/events/<backend>.py` implementing `EventBus`
   (`src/core/ports/event_bus.py`) — including slow-consumer disconnection and
   best-effort replay.
2. Add a branch in `src/deps.py::_get_event_bus` and an entry in
   `_REQUIRED_EVENT_BUS_ENV` (a commented stub shows where).
3. Set `EVENT_BUS_BACKEND=<backend>` in your Modal secret.

Provide a shared `WebhookEventStore` the same way (`record_if_new` must be
atomic — e.g. `SET NX` with a TTL).
