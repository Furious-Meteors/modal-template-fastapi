"""
src/adapters/telemetry/stream_metrics.py
========================================
Connection-level instruments for long-lived transports (SSE, WebSocket) and for
inbound webhooks. Recorded by the telemetry sidecar (TelemetryMiddleware in
src/adapters/http/middleware.py) from what it observes at the HTTP/WebSocket
boundary — the endpoints themselves contain no telemetry code.

There is deliberately no per-message span: one span per connection is recorded by
TelemetryMiddleware, and per-message tracing would be costly and noisy.

Instruments (all labels are small fixed sets):

  stream.connections.active   up-down  transport = sse|websocket
  stream.connections.closed   counter  transport, reason
        sse:       client_disconnect | server_closed | error
        websocket: client_disconnect | max_duration | rate_limited |
                   message_too_large | server_busy | rejected | error | other
  stream.messages.sent        counter  transport — protocol frames written to the
                              client (SSE body chunks, WebSocket messages),
                              heartbeats included
  webhook.received            counter  provider, outcome
        accepted | duplicate | bad_signature | stale | bad_timestamp |
        bad_request | too_large | rate_limited | error
        `provider` is only ever a configured provider (see observe_webhook).

For "why was this consumer dropped" see app.events.subscriptions.dropped, recorded
by MeteredEventBus: only the bus knows a consumer fell behind.

Instruments are created lazily (get()) so they bind to the real MeterProvider
installed after a Modal snapshot restore. Every record method swallows
exceptions — telemetry never breaks a stream.
"""
from __future__ import annotations

import logging
from typing import Optional

from opentelemetry.metrics import Meter

from src.infrastructure.setup import get_meter

logger = logging.getLogger(__name__)


class StreamMetrics:
    def __init__(self, meter: Meter) -> None:
        self._active = meter.create_up_down_counter(
            "stream.connections.active", description="Open SSE/WebSocket connections", unit="1"
        )
        self._closed = meter.create_counter(
            "stream.connections.closed", description="Closed stream connections by reason", unit="1"
        )
        self._messages = meter.create_counter(
            "stream.messages.sent", description="Frames/messages written to stream clients", unit="1"
        )
        self._webhooks = meter.create_counter(
            "webhook.received", description="Inbound webhooks by provider and outcome", unit="1"
        )

    def stream_opened(self, transport: str) -> None:
        try:
            self._active.add(1, {"transport": transport})
        except Exception:
            logger.debug("stream metric failed", exc_info=True)

    def stream_closed(self, transport: str, reason: str) -> None:
        try:
            self._active.add(-1, {"transport": transport})
            self._closed.add(1, {"transport": transport, "reason": reason})
        except Exception:
            logger.debug("stream metric failed", exc_info=True)

    def message_sent(self, transport: str) -> None:
        try:
            self._messages.add(1, {"transport": transport})
        except Exception:
            logger.debug("stream metric failed", exc_info=True)

    def webhook_outcome(self, provider: str, outcome: str) -> None:
        try:
            self._webhooks.add(1, {"provider": provider, "outcome": outcome})
        except Exception:
            logger.debug("webhook metric failed", exc_info=True)


_instance: Optional[StreamMetrics] = None


def get() -> StreamMetrics:
    """Process-wide instruments, created on first use."""
    global _instance
    if _instance is None:
        _instance = StreamMetrics(get_meter())
    return _instance


# ── What the sidecar derives from the wire ──────────────────────────────────
# The reasons below come from protocol facts (close codes, statuses, the stable
# error_code the error handler records on the request), so the endpoints do not
# need to tell the telemetry anything.

_WS_CLOSE_REASONS = {
    1001: "max_duration",
    1008: "rate_limited",
    1009: "message_too_large",
    1013: "server_busy",
    4400: "rejected",
    4401: "rejected",
    4403: "rejected",
}


def websocket_close_reason(*, server_close_code: Optional[int], errored: bool) -> str:
    """Why a WebSocket connection ended. No server-sent close code ⇒ the client left."""
    if errored:
        return "error"
    if server_close_code is None:
        return "client_disconnect"
    return _WS_CLOSE_REASONS.get(server_close_code, "other")


_WEBHOOK_ERROR_OUTCOMES = {
    "MISSING_SIGNATURE": "bad_signature",
    "INVALID_SIGNATURE": "bad_signature",
    "STALE_TIMESTAMP": "stale",
    "MISSING_TIMESTAMP": "bad_timestamp",
    "INVALID_TIMESTAMP": "bad_timestamp",
    "MISSING_WEBHOOK_ID": "bad_request",
    "INVALID_PAYLOAD": "bad_request",
    "PAYLOAD_TOO_LARGE": "too_large",
    "RATE_LIMITED": "rate_limited",
}


def observe_webhook(provider: Optional[str], status: int, error_code: Optional[str]) -> None:
    """
    Record one webhook delivery from its response. `provider` comes from the URL,
    which anyone can set, so it is only used as a label when the response proves
    the provider is configured. Unknown providers (404) and wrong methods (405)
    are not recorded — labelling them would let any prober create unlimited series.
    """
    if not provider or status in (404, 405):
        return
    if status == 202:
        outcome = "accepted"
    elif status == 200:
        outcome = "duplicate"
    elif status >= 500:
        outcome = "error"
    else:
        outcome = _WEBHOOK_ERROR_OUTCOMES.get(error_code or "")
        if outcome is None:
            return
    get().webhook_outcome(provider, outcome)
