"""
src/adapters/telemetry/stream_metrics.py
========================================
Connection-level metrics for long-lived transports (SSE, WebSocket). There is
deliberately no per-message span — one span per connection is recorded by
TelemetryMiddleware, and per-message tracing would be costly and noisy.

Instruments are created lazily for the same reason as in the HTTP middleware:
they must bind to the real MeterProvider set up in Modal's startup() hook.
Every call swallows exceptions — telemetry never breaks a stream.
"""
from __future__ import annotations

import logging

from src.infrastructure.setup import get_meter

logger = logging.getLogger(__name__)

_instruments: dict | None = None


def _get() -> dict:
    global _instruments
    if _instruments is None:
        meter = get_meter()
        _instruments = {
            "active": meter.create_up_down_counter(
                "stream.connections.active",
                description="Open SSE/WebSocket connections",
                unit="1",
            ),
            "messages": meter.create_counter(
                "stream.messages.sent",
                description="Messages pushed to stream clients",
                unit="1",
            ),
            "closed": meter.create_counter(
                "stream.connections.closed",
                description="Closed stream connections by reason",
                unit="1",
            ),
            "webhooks": meter.create_counter(
                "webhook.received",
                description="Inbound webhooks by provider and outcome",
                unit="1",
            ),
        }
    return _instruments


def stream_opened(transport: str) -> None:
    try:
        _get()["active"].add(1, {"transport": transport})
    except Exception:
        logger.debug("stream metric failed", exc_info=True)


def stream_closed(transport: str, reason: str) -> None:
    try:
        inst = _get()
        inst["active"].add(-1, {"transport": transport})
        inst["closed"].add(1, {"transport": transport, "reason": reason})
    except Exception:
        logger.debug("stream metric failed", exc_info=True)


def message_sent(transport: str) -> None:
    try:
        _get()["messages"].add(1, {"transport": transport})
    except Exception:
        logger.debug("stream metric failed", exc_info=True)


def webhook_outcome(provider: str, outcome: str) -> None:
    # `provider` is only ever a configured provider (unknown ones 404 before
    # this is called), so label cardinality stays bounded.
    try:
        _get()["webhooks"].add(1, {"provider": provider, "outcome": outcome})
    except Exception:
        logger.debug("webhook metric failed", exc_info=True)
