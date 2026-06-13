"""
src/adapters/http/middleware.py
================================
Inbound HTTP adapter — telemetry sidecar at the HTTP boundary.
Wraps every request in an OTel span, records metrics, and emits structured logs.
Add once in main.py — no per-route changes needed.

    from src.adapters.http.middleware import TelemetryMiddleware
    app.add_middleware(TelemetryMiddleware)

What is recorded automatically per request
───────────────────────────────────────────
Trace span
  • Name:   "GET /api/v1/items/{item_id}"  (route template, not raw URL)
  • Tags:   method, route, status_code, client_ip, user_agent, session_id
  • Status: ERROR on 4xx/5xx, OK otherwise

Metrics  (all labelled by method + route + status_class)
  • http.server.request.count          counter
  • http.server.request.duration       histogram (ms)   ← p50/p95/p99 source
  • http.server.active_requests        up-down counter  ← in-flight gauge
  • http.server.error.count            counter  (4xx+5xx only)
  • http.server.response.size          histogram (bytes)

Performance contract
────────────────────
Hot path cost per request: ~2–5µs
  • span context push    — pointer assignment
  • counter increment    — atomic int add
  • histogram record     — single float append to bounded buffer

All flushing to Grafana Cloud happens in a background thread
via BatchSpanProcessor. A full queue or unreachable endpoint
drops data silently — the request is never blocked or errored.
"""

from __future__ import annotations

import logging
import time
import uuid

from opentelemetry import propagate, trace
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace import SpanKind, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

from src.infrastructure.setup import get_meter, get_tracer

logger = logging.getLogger(__name__)

# ── Lazy instrument initialisation ─────────────────────────────────────────
# Instruments are created on the first request, not at module import time.
# This guarantees they are bound to the real MeterProvider set by
# setup_telemetry() in Modal's startup() hook — not to the no-op provider
# that exists at snapshot / import time.

_instruments: dict | None = None


def _get_instruments() -> dict:
    global _instruments
    if _instruments is None:
        meter = get_meter()
        _instruments = {
            "request_count": meter.create_counter(
                "http.server.request.count",
                description="Total HTTP requests received",
                unit="1",
            ),
            "request_duration": meter.create_histogram(
                "http.server.request.duration",
                description="HTTP request wall-clock duration",
                unit="ms",
            ),
            "active_requests": meter.create_up_down_counter(
                "http.server.active_requests",
                description="HTTP requests currently in flight",
                unit="1",
            ),
            "error_count": meter.create_counter(
                "http.server.error.count",
                description="HTTP responses with 4xx or 5xx status",
                unit="1",
            ),
            "response_size": meter.create_histogram(
                "http.server.response.size",
                description="HTTP response body size",
                unit="By",
            ),
        }
    return _instruments


# ── Helpers ────────────────────────────────────────────────────────────────

def _route_template(request: Request) -> str:
    """
    Return the matched route pattern, e.g. '/api/v1/items/{item_id}'.
    Falls back to raw path on 404/unmatched routes.
    Using the template (not raw path) prevents metric cardinality explosion
    from path params like /items/1 /items/2 /items/3 …
    """
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", request.url.path)
    return request.url.path


def _labels(method: str, route: str, status: int) -> dict[str, str]:
    return {
        "method":       method,
        "route":        route,
        "status_class": f"{status // 100}xx",
    }


# ── Middleware class ────────────────────────────────────────────────────────

class TelemetryMiddleware(BaseHTTPMiddleware):
    """
    Drop-in OTel middleware. No constructor args needed.

    Failure-safe: any exception inside the instrumentation path is caught
    and logged at DEBUG level. The request always continues.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        tracer  = get_tracer()
        instr   = _get_instruments()
        route   = _route_template(request)
        method  = request.method
        t_start = time.perf_counter()
        # Generate once per request so the span always has a non-empty session_id.
        # setdefault below lets a route override this with its own value if it sets
        # the x-session-id header explicitly — the middleware value is the fallback.
        req_session_id = str(uuid.uuid4())

        # ── In-flight gauge ───────────────────────────────────────────
        try:
            instr["active_requests"].add(1, {"method": method, "route": route})
        except Exception:
            pass

        # Extract W3C TraceContext from incoming headers so this span becomes a
        # child of any upstream trace (e.g. a caller service that passed traceparent).
        # Falls back to a new root span when no traceparent header is present.
        parent_ctx = propagate.extract(request.headers)

        with tracer.start_as_current_span(
            f"{method} {route}",
            kind=SpanKind.SERVER,
            context=parent_ctx,
        ) as span:
            # Standard HTTP semantic-convention attributes
            span.set_attribute(SpanAttributes.HTTP_METHOD,     method)
            span.set_attribute(SpanAttributes.HTTP_TARGET,     request.url.path)
            span.set_attribute(SpanAttributes.HTTP_ROUTE,      route)
            span.set_attribute(SpanAttributes.HTTP_SCHEME,     request.url.scheme)
            span.set_attribute(SpanAttributes.NET_HOST_NAME,   request.url.hostname or "")
            span.set_attribute(SpanAttributes.HTTP_USER_AGENT, request.headers.get("user-agent", ""))
            span.set_attribute(
                SpanAttributes.HTTP_CLIENT_IP,
                request.client.host if request.client else "unknown",
            )

            # ── Call the route handler ────────────────────────────────
            try:
                response: Response = await call_next(request)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                _record(instr, method, route, 500, (time.perf_counter() - t_start) * 1000, 0)
                instr["active_requests"].add(-1, {"method": method, "route": route})
                raise

            # ── Annotate span ─────────────────────────────────────────
            status = response.status_code
            span.set_attribute(SpanAttributes.HTTP_STATUS_CODE, status)

            # Guarantee x-session-id is always present on the response so the span
            # is never empty and callers can correlate by this header.
            response.headers.setdefault("x-session-id", req_session_id)
            session_id = response.headers.get("x-session-id", "")
            if session_id:
                span.set_attribute("app.session_id", session_id)

            if status >= 400:
                span.set_status(StatusCode.ERROR, f"HTTP {status}")
            else:
                span.set_status(StatusCode.OK)

            # ── Record metrics ────────────────────────────────────────
            duration_ms    = (time.perf_counter() - t_start) * 1000
            content_length = int(response.headers.get("content-length", 0))
            _record(instr, method, route, status, duration_ms, content_length)

            # ── Structured log ────────────────────────────────────────
            ctx       = span.get_span_context()
            trace_hex = format(ctx.trace_id, "032x") if ctx else ""
            level     = logging.WARNING if status >= 400 else logging.INFO
            logger.log(
                level,
                "%s %s → %d  %.1fms  trace=%s",
                method, request.url.path, status, duration_ms, trace_hex,
                extra={
                    "http.method":      method,
                    "http.route":       route,
                    "http.status_code": status,
                    "http.duration_ms": round(duration_ms, 2),
                    "trace_id":         trace_hex,
                    "session_id":       session_id,
                },
            )

            instr["active_requests"].add(-1, {"method": method, "route": route})
            return response


def _record(
    instr: dict,
    method: str,
    route: str,
    status: int,
    duration_ms: float,
    response_bytes: int,
) -> None:
    """Record all per-request metrics. Swallows exceptions silently."""
    try:
        lb = _labels(method, route, status)
        instr["request_count"].add(1, lb)
        instr["request_duration"].record(duration_ms, lb)
        if status >= 400:
            instr["error_count"].add(1, lb)
        if response_bytes > 0:
            instr["response_size"].record(response_bytes, lb)
    except Exception:
        logger.debug("Metric recording failed", exc_info=True)
