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
  • WebSocket connections get one span per connection ("WEBSOCKET /api/v1/ws");
    there is deliberately no per-message span.

Stream and webhook metrics (see adapters/telemetry/stream_metrics.py)
  Recorded here, from the wire, so the endpoints contain no telemetry code:
  SSE is recognised by the response content-type, WebSocket by scope type; close
  reasons come from close codes and disconnect messages; webhook outcomes from the
  response status plus the stable error_code the error handler stores on the
  request (request.state.error_code).

Implemented as a pure ASGI middleware (not BaseHTTPMiddleware) so streaming
responses (SSE) pass through unbuffered, client disconnects propagate to the
endpoint, and WebSocket scopes are supported.

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

from opentelemetry import propagate
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace import SpanKind, StatusCode
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.adapters.telemetry import stream_metrics
from src.infrastructure.setup import get_meter, get_tracer
from src.utils.config import SERVER_PREFIX

logger = logging.getLogger(__name__)

# ── Lazy instrument initialisation ─────────────────────────────────────────
# Instruments are created on the first request, not at module import time.
# This guarantees they are bound to the real MeterProvider set by
# setup_telemetry() in Modal's startup() hook — not to the no-op provider
# that exists at snapshot / import time.

_instruments: dict | None = None

UNMATCHED_ROUTE = "unmatched"


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

def _route_template(conn: HTTPConnection) -> str:
    """
    Return the matched route pattern, e.g. '/api/v1/items/{item_id}'.

    Must be called AFTER the request has been handled: FastAPI records the
    matched route in scope["route"] (HTTP routes without the include_router
    prefix, which is re-attached here). Plain Starlette routes such as /docs are found by
    matching. Everything else — 404s, scanners probing arbitrary URLs — shares
    the fixed label UNMATCHED_ROUTE, so metric cardinality is bounded by the
    number of real routes rather than by what clients send.
    """
    scope = conn.scope
    matched = scope.get("route")
    path = getattr(matched, "path", None)
    if path:
        # HTTP routes record their path without the include_router prefix but
        # WebSocket routes record it in full, so only add it when it is missing.
        if (
            SERVER_PREFIX
            and scope["path"].startswith(SERVER_PREFIX)
            and not path.startswith(SERVER_PREFIX)
        ):
            return f"{SERVER_PREFIX}{path}"
        return path
    app = scope.get("app")
    for route in getattr(app, "routes", ()):
        route_path = getattr(route, "path", None)
        if route_path and route.matches(scope)[0] == Match.FULL:
            return route_path
    return UNMATCHED_ROUTE


def _safe_route(conn: HTTPConnection) -> str:
    """
    _route_template that can never fail a request. It leans on FastAPI/Starlette
    scope internals, so if an upgrade changes their shape we degrade to the
    "unmatched" label (tests/test_route_labels.py then fails loudly in CI)
    instead of raising after the response has already been sent.
    """
    try:
        return _route_template(conn)
    except Exception:
        logger.warning("Could not resolve route template; using %r", UNMATCHED_ROUTE, exc_info=True)
        return UNMATCHED_ROUTE


def _labels(method: str, route: str, status: int) -> dict[str, str]:
    return {
        "method":       method,
        "route":        route,
        "status_class": f"{status // 100}xx",
    }


# ── Middleware class ────────────────────────────────────────────────────────

class TelemetryMiddleware:
    """
    Drop-in OTel middleware. No constructor args needed beyond the ASGI app.

    Failure-safe: any exception inside the instrumentation path is caught
    and logged at DEBUG level. The request always continues.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:  # lifespan
            await self.app(scope, receive, send)

    # ── shared setup ──────────────────────────────────────────────────
    @staticmethod
    def _instruments() -> dict:
        # Instrumentation must never fail a request: fall back to no-op stand-ins.
        try:
            return _get_instruments()
        except Exception:
            logger.debug("Telemetry instruments unavailable", exc_info=True)
            return _NOOP_INSTRUMENTS

    @staticmethod
    def _start(scope: Scope) -> tuple[HTTPConnection, str]:
        # Single source of truth for the request's session ID. Routes and error
        # handlers read it from request.state.session_id, so the response body,
        # the x-session-id header and the span attribute always agree.
        session_id = str(uuid.uuid4())
        scope.setdefault("state", {})["session_id"] = session_id
        return HTTPConnection(scope), session_id

    @staticmethod
    def _set_request_attributes(span, conn: HTTPConnection, method: str) -> None:
        span.set_attribute(SpanAttributes.HTTP_METHOD,     method)
        span.set_attribute(SpanAttributes.HTTP_TARGET,     conn.url.path)
        span.set_attribute(SpanAttributes.HTTP_SCHEME,     conn.url.scheme)
        span.set_attribute(SpanAttributes.NET_HOST_NAME,   conn.url.hostname or "")
        span.set_attribute(SpanAttributes.HTTP_USER_AGENT, conn.headers.get("user-agent", ""))
        span.set_attribute(
            SpanAttributes.HTTP_CLIENT_IP,
            conn.client.host if conn.client else "unknown",
        )

    # ── HTTP ──────────────────────────────────────────────────────────
    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        instr = self._instruments()
        tracer = get_tracer()
        conn, session_id = self._start(scope)
        method = scope["method"]
        t_start = time.perf_counter()
        active_labels = {"method": method}  # the route is only known after handling
        state = {"status": 500, "bytes": 0, "sse": False, "done": False, "client_gone": False, "errored": False}
        streams = stream_metrics.get()

        async def receive_wrapper() -> Message:
            message = await receive()
            # A disconnect that arrives after the final body chunk is just the
            # client closing a response the server already finished.
            if message["type"] == "http.disconnect" and not state["done"]:
                state["client_gone"] = True
            return message

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                # Always present on the response so callers can correlate by this header.
                headers = MutableHeaders(scope=message)
                headers["x-session-id"] = session_id
                if message["status"] == 200 and headers.get("content-type", "").startswith("text/event-stream"):
                    state["sse"] = True
                    streams.stream_opened("sse")
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                state["bytes"] += len(body)
                if not message.get("more_body", False):
                    state["done"] = True
                if state["sse"] and body:
                    streams.message_sent("sse")
            await send(message)

        _safe_add(instr["active_requests"], 1, active_labels)
        try:
            # Extract W3C TraceContext from incoming headers so this span becomes a
            # child of any upstream trace (e.g. a caller service that passed traceparent).
            # Falls back to a new root span when no traceparent header is present.
            parent_ctx = propagate.extract(conn.headers)

            with tracer.start_as_current_span(
                method, kind=SpanKind.SERVER, context=parent_ctx
            ) as span:
                self._set_request_attributes(span, conn, method)
                span.set_attribute("app.session_id", session_id)
                try:
                    await self.app(scope, receive_wrapper, send_wrapper)
                except Exception as exc:
                    state["errored"] = True
                    route = _safe_route(conn)
                    span.update_name(f"{method} {route}")
                    span.set_attribute(SpanAttributes.HTTP_ROUTE, route)
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                    # If the response had already started, the real status stands.
                    _record(instr, method, route, state["status"],
                            (time.perf_counter() - t_start) * 1000, state["bytes"])
                    self._observe_webhook(scope, route, state["status"])
                    raise

                route = _safe_route(conn)
                status = state["status"]
                span.update_name(f"{method} {route}")
                span.set_attribute(SpanAttributes.HTTP_ROUTE, route)
                span.set_attribute(SpanAttributes.HTTP_STATUS_CODE, status)
                span.set_status(
                    StatusCode.ERROR if status >= 400 else StatusCode.OK,
                    f"HTTP {status}" if status >= 400 else None,
                )

                duration_ms = (time.perf_counter() - t_start) * 1000
                _record(instr, method, route, status, duration_ms, state["bytes"])
                self._observe_webhook(scope, route, status)

                ctx       = span.get_span_context()
                trace_hex = format(ctx.trace_id, "032x") if ctx else ""
                logger.log(
                    logging.WARNING if status >= 400 else logging.INFO,
                    "%s %s → %d  %.1fms  trace=%s",
                    method, conn.url.path, status, duration_ms, trace_hex,
                    extra={
                        "http.method":      method,
                        "http.route":       route,
                        "http.status_code": status,
                        "http.duration_ms": round(duration_ms, 2),
                        "trace_id":         trace_hex,
                        "session_id":       session_id,
                    },
                )
        finally:
            # Runs on every exit path so the in-flight gauge can never leak.
            _safe_add(instr["active_requests"], -1, active_labels)
            if state["sse"]:
                # Order matters: a client that vanishes can also surface as a send error.
                reason = (
                    "client_disconnect" if state["client_gone"]
                    else "error" if state["errored"]
                    else "server_closed"
                )
                streams.stream_closed("sse", reason)

    @staticmethod
    def _observe_webhook(scope: Scope, route: str, status: int) -> None:
        if not route.endswith("/webhooks/{provider}"):
            return
        try:
            stream_metrics.observe_webhook(
                scope.get("path_params", {}).get("provider"),
                status,
                scope.get("state", {}).get("error_code"),
            )
        except Exception:
            logger.debug("webhook metric failed", exc_info=True)

    # ── WebSocket ─────────────────────────────────────────────────────
    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        tracer = get_tracer()
        conn, session_id = self._start(scope)
        state: dict = {"close_code": None, "opened": False, "server_close_code": None, "errored": False}
        streams = stream_metrics.get()

        async def receive_wrapper() -> Message:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                state["close_code"] = message.get("code")
            return message

        async def send_wrapper(message: Message) -> None:
            kind = message["type"]
            if kind == "websocket.accept":
                state["opened"] = True
                streams.stream_opened("websocket")
            elif kind == "websocket.send":
                streams.message_sent("websocket")
            elif kind == "websocket.close":
                state["close_code"] = state["server_close_code"] = message.get("code", 1000)
            await send(message)

        parent_ctx = propagate.extract(conn.headers)
        with tracer.start_as_current_span(
            "WEBSOCKET", kind=SpanKind.SERVER, context=parent_ctx
        ) as span:
            self._set_request_attributes(span, conn, "WEBSOCKET")
            span.set_attribute("app.session_id", session_id)
            try:
                await self.app(scope, receive_wrapper, send_wrapper)
            except Exception as exc:
                state["errored"] = True
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise
            finally:
                if state["opened"]:
                    streams.stream_closed(
                        "websocket",
                        stream_metrics.websocket_close_reason(
                            server_close_code=state["server_close_code"], errored=state["errored"]
                        ),
                    )
                route = _safe_route(conn)
                span.update_name(f"WEBSOCKET {route}")
                span.set_attribute(SpanAttributes.HTTP_ROUTE, route)
                if state["close_code"] is not None:
                    span.set_attribute("websocket.close_code", state["close_code"])
            logger.info(
                "WEBSOCKET %s closed code=%s", conn.url.path, state["close_code"],
                extra={"http.route": route, "session_id": session_id},
            )


def _safe_add(instrument, value: int, labels: dict[str, str]) -> None:
    try:
        instrument.add(value, labels)
    except Exception:
        logger.debug("Metric recording failed", exc_info=True)


class _NoopInstrument:
    def add(self, *args, **kwargs) -> None: ...
    def record(self, *args, **kwargs) -> None: ...


_NOOP_INSTRUMENTS = {
    k: _NoopInstrument()
    for k in ("request_count", "request_duration", "active_requests", "error_count", "response_size")
}


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
