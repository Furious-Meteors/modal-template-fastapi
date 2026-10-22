# Streaming & Webhooks

Three ways to move events across the API boundary. All three sit on one
`EventBus` port, so swapping the backend never touches a route.

| | Direction | Auth | Use for |
|---|---|---|---|
| [Server-Sent Events](#server-sent-events) | server → client | Bearer JWT, `events:read` | Dashboards, live feeds; browsers reconnect for free |
| [WebSocket](#websocket) | both | short-lived ticket | Interactive clients that also send messages |
| [Webhooks](#webhooks) | third party → server | HMAC signature | Receiving events from other systems |

!!! warning "Set `EVENT_BUS_BACKEND=postgres` before running more than one container"
    `EVENT_BUS_BACKEND` (default `memory`) selects the **streaming state** as a
    unit — the event bus, webhook de-duplication and WebSocket ticket replay
    protection. With `memory` each is private to a container: a stream on
    container A won't see events published on B, duplicate webhooks landing on
    different containers aren't detected, and a ticket can be used once per
    container. Production logs a warning at startup. With `postgres` all three
    are shared — see [Persistence → Shared streaming state](../persistence.md#shared-streaming-state).

## Topics and events

Streams subscribe to a **topic**: `items` (default) or `webhooks`.

Subscribing needs `events:read` **plus the topic's scope**, so a stream never
reveals what the REST API would deny:

| Topic | Extra scope |
|---|---|
| `items` | `items:read` |
| `webhooks` | `webhooks:read` (third-party payloads — grant deliberately) |

For ownership-style rules, `src/api/event_access.py::can_view(event, user)` is
called for every event and subscriber (default: allow). Override it when events
belong to individual users.

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
* **Behind Modal's proxy the app is not told when a client disconnects** — it finds out
  when its next heartbeat write fails. A closed stream therefore holds its connection-cap
  slot for up to one heartbeat (`STREAM_HEARTBEAT_SECONDS`; Modal deployments use 5 s,
  measured live: ~10 s with the old 15 s setting, ~0.7 s after the change). A client that
  reconnects immediately may get `503` with `Retry-After: 5`; it should wait and retry,
  which `EventSource` does by itself.

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
| `1008` | client sent messages faster than `RATE_WS_MESSAGES_PER_SECOND` | slow down |
| `4403` | ticket lacks `events:read` or the topic's scope | — |

**Ticket design.** A ticket is a signed JWT with its own audience, so it cannot
be used as an API token (and API tokens cannot be used as tickets). It carries
only your *read* scopes (`events:read`, `items:read`, `webhooks:read` — never a
write scope). Verification is stateless; single use is enforced by a replay store
that is shared across containers with `EVENT_BUS_BACKEND=postgres` (per container
with `memory`). The 30 s lifetime bounds any residual window.

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
| under the per-provider rate limit | `429 RATE_LIMITED` (+ `Retry-After`) |
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

## Rate limits

In-memory token buckets, **per container** (N containers ⇒ up to N× the rate) —
abuse protection, not a hard global quota; enforce those at the edge.

| Where | Key | Default |
|---|---|---|
| `POST /ws/ticket` | authenticated user | `RATE_TICKET_PER_MINUTE=30` |
| `POST /webhooks/{provider}` | provider | `RATE_WEBHOOK_PER_MINUTE=1200` (providers retry a 429) |
| WebSocket client messages | connection | `RATE_WS_MESSAGES_PER_SECOND=10` |

Keys are never client IPs: behind a proxy one IP can be everybody, and limiting
failed signatures per IP would let an attacker lock out the real sender.

## Configuration

| Variable | Default | Set by |
|---|---|---|
| `EVENT_BUS_BACKEND` | `memory` | `memory` or `postgres` — see [Persistence](../persistence.md#shared-streaming-state) |
| `EVENT_BUS_LISTEN_URL` | unset | Session-level Postgres URL for instant delivery (postgres only) |
| `EVENT_BUS_POLL_MS` | 1000 | Poll interval (postgres; safety net when LISTEN is on) |
| `EVENT_RETENTION_SECONDS` / `EVENT_REPLAY_SIZE` | 3600 / 100 | Event log retention and replay depth (postgres) |
| `WEBHOOK_IDEMPOTENCY_TTL_SECONDS` | 86400 | How long a webhook id is remembered |
| `RATE_TICKET_PER_MINUTE` / `RATE_WEBHOOK_PER_MINUTE` / `RATE_WS_MESSAGES_PER_SECOND` | 30 / 1200 / 10 | See [Rate limits](#rate-limits) |
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
* **Stream and webhook metrics are recorded by the telemetry sidecar**
  (`TelemetryMiddleware`) from what it sees on the wire — the endpoints contain no
  telemetry code:

  | Metric | Labels | Notes |
  |---|---|---|
  | `stream.connections.active` | `transport` = `sse`/`websocket` | up-down gauge |
  | `stream.connections.closed` | `transport`, `reason` | SSE: `client_disconnect`, `server_closed`, `error`. WebSocket: `client_disconnect`, `max_duration` (1001), `rate_limited` (1008), `message_too_large` (1009), `server_busy` (1013), `rejected` (4400/4401/4403), `error`, `other` |
  | `stream.messages.sent` | `transport` | protocol frames written to the client — SSE body chunks, WebSocket messages — **heartbeats included** |
  | `webhook.received` | `provider`, `outcome` | `accepted`, `duplicate`, `bad_signature`, `stale`, `bad_timestamp`, `bad_request`, `too_large`, `rate_limited`, `error` |
  | `app.events.subscriptions.dropped` | `topic`, `reason=ended_by_bus` | the bus ended a subscription because its consumer fell behind |

  Reasons come from protocol facts (close codes, disconnect messages, the response
  status and the stable `error_code` the error handler stores on the request). Two
  consequences: an SSE stream the server ended is just `server_closed` (the
  sidecar cannot tell the lifetime limit from a slow consumer — the *dropped*
  counter above is how slow consumers show up), and WebSocket `rejected`
  connections are counted as opened-then-closed because the handshake is accepted
  before the ticket is checked. `provider` is only labelled when the response proves
  the provider is configured, so URL probing cannot create series.
* Business metrics for the bus come from the `MeteredEventBus` sidecar:
  `app.events.published`, `app.events.publish.duration`, `app.events.delivery.lag`
  (event creation → delivery to a stream consumer, so it includes cross-container
  latency; with the Postgres bus the timestamp is the database server's clock) and
  `app.events.replayed`.
* FastAPI's own request telemetry is **disabled** (`FASTAPI_NATIVE_TELEMETRY`,
  default off): left on it adds a second server span per request and a second
  `http.server.request.duration` in seconds beside ours in ms. The cost is losing
  its `fastapi.dependencies/endpoint/serialization` child spans. Set
  `FASTAPI_NATIVE_TELEMETRY=true` to restore them (you then get both sets).

## Adding another event-bus backend

1. Create `src/adapters/events/<backend>.py` implementing `EventBus`
   (`src/core/ports/event_bus.py`), `WebhookEventStore` and `TicketReplayStore`
   (atomic `record_if_new` / `consume` — e.g. `SET NX` with a TTL). Reuse
   `tests/test_event_bus.py` and `tests/test_shared_state_contract.py`: add the
   backend to the fixtures and it must pass the same contract.
2. Add branches in `src/deps.py` (`_get_event_bus`, `_get_webhook_store`,
   `_get_ticket_store`) and an entry in `_REQUIRED_EVENT_BUS_ENV`.
3. Set `EVENT_BUS_BACKEND=<backend>` in your Modal secret.
