"""
src/infrastructure/metrics.py
=============================
Business-level metric instruments (service-owned — replace per service).

HTTP-level signals (request count, latency, errors, in-flight) come from
TelemetryMiddleware; stream/webhook connection metrics from stream_metrics.
This file defines what the *business* wants to know. It only DEFINES and RECORDS
instruments — nothing in core/ or the routes ever calls it. The telemetry
sidecars in src/adapters/telemetry/metered.py observe the service and the event
bus from the outside and call these helpers, wired in src/deps.py exactly like
TracingProxy.

Contract
────────
• Names are "app.<area>.<thing>", dot-notation; labels are added at record time.
• Every label has a small, fixed set of values (listed below). Never put an id,
  user, path or free text in a label — that is how metric cardinality explodes.
• Recording never raises: telemetry must not break the code it observes.
• Instruments are created lazily on first use (get()), so they bind to the real
  MeterProvider that setup_telemetry() installs after a Modal snapshot restore.

Instruments
───────────
app.items.operations           counter    operation = create|get|list|update|delete
                                          outcome   = ok|not_found|conflict|unavailable|error
app.items.operation.duration   histogram  ms, by operation and outcome
app.events.published           counter    topic, type (webhook.<provider> → "webhook"), outcome = ok|error
app.events.publish.duration    histogram  ms, by topic and outcome
app.events.delivery.lag        histogram  ms from Event.ts to delivery to a stream consumer, by topic
app.events.replayed            counter    events served from replay (Last-Event-ID), by topic
app.events.subscriptions.dropped counter  topic, reason=ended_by_bus — the bus ended a subscription
                                          because its consumer fell behind (or the bus shut down)

Delivery lag compares the event's timestamp with the consumer's clock. With the
Postgres bus the timestamp is the database server's, so lag includes any clock
difference between database and container (negative values are clamped to 0).

Adding a metric: add the instrument below, add a record_* method, call it from
the relevant sidecar in src/adapters/telemetry/metered.py, and add a test.
"""
from __future__ import annotations

import logging
from typing import Optional

from opentelemetry.metrics import Meter

from src.infrastructure.setup import get_meter

logger = logging.getLogger(__name__)


def _event_type_label(event_type: str) -> str:
    """Bound cardinality: webhook.<provider> collapses to one value."""
    return "webhook" if event_type.startswith("webhook.") else event_type


class BusinessMetrics:
    def __init__(self, meter: Meter) -> None:
        self._item_ops = meter.create_counter(
            "app.items.operations",
            description="Item service operations by outcome",
            unit="1",
        )
        self._item_op_duration = meter.create_histogram(
            "app.items.operation.duration",
            description="Item service operation duration",
            unit="ms",
        )
        self._events_published = meter.create_counter(
            "app.events.published",
            description="Events published to the bus",
            unit="1",
        )
        self._publish_duration = meter.create_histogram(
            "app.events.publish.duration",
            description="Time to publish one event to the bus",
            unit="ms",
        )
        self._delivery_lag = meter.create_histogram(
            "app.events.delivery.lag",
            description="Time from event creation to delivery to a stream consumer",
            unit="ms",
        )
        self._replayed = meter.create_counter(
            "app.events.replayed",
            description="Events delivered from replay after a reconnect",
            unit="1",
        )
        self._dropped = meter.create_counter(
            "app.events.subscriptions.dropped",
            description="Subscriptions ended by the bus itself (a consumer fell behind)",
            unit="1",
        )

    # Every method swallows exceptions: observing must never break the observed.

    def record_item_op(self, operation: str, outcome: str, duration_ms: float) -> None:
        try:
            labels = {"operation": operation, "outcome": outcome}
            self._item_ops.add(1, labels)
            self._item_op_duration.record(duration_ms, labels)
        except Exception:
            logger.debug("metric recording failed", exc_info=True)

    def record_event_published(self, topic: str, event_type: str, outcome: str, duration_ms: float) -> None:
        try:
            self._events_published.add(
                1, {"topic": topic, "type": _event_type_label(event_type), "outcome": outcome}
            )
            self._publish_duration.record(duration_ms, {"topic": topic, "outcome": outcome})
        except Exception:
            logger.debug("metric recording failed", exc_info=True)

    def record_event_delivered(self, topic: str, lag_ms: float, replayed: bool) -> None:
        try:
            if replayed:
                self._replayed.add(1, {"topic": topic})
            else:
                self._delivery_lag.record(max(0.0, lag_ms), {"topic": topic})
        except Exception:
            logger.debug("metric recording failed", exc_info=True)


    def record_subscription_dropped(self, topic: str) -> None:
        try:
            self._dropped.add(1, {"topic": topic, "reason": "ended_by_bus"})
        except Exception:
            logger.debug("metric recording failed", exc_info=True)


_instance: Optional[BusinessMetrics] = None


def get() -> BusinessMetrics:
    """Process-wide instruments, created on first use."""
    global _instance
    if _instance is None:
        _instance = BusinessMetrics(get_meter())
    return _instance
