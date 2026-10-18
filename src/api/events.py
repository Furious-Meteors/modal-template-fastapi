"""
src/api/events.py
=================
Server-push endpoints over the EventBus port:

  GET  /events/stream   Server-Sent Events (Bearer JWT, scope events:read)
  POST /ws/ticket       trade a Bearer JWT for a short-lived WebSocket ticket
  WS   /ws?ticket=...   WebSocket stream

Both transports share a per-container connection cap and end themselves after
STREAM_MAX_DURATION_SECONDS so clients reconnect (SSE: Last-Event-ID; WebSocket:
?last_event_id=) well inside Modal's request timeout.

WebSocket close codes:
  1001 max duration reached (reconnect)   1009 client message too large
  1013 server busy / slow consumer        4401 missing/invalid ticket
  4403 ticket lacks events:read           4400 unknown topic
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import AsyncIterator, Literal, Optional

from fastapi import APIRouter, Depends, Header, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from src.adapters.telemetry import stream_metrics
from src.api import streaming
from src.api.auth import create_ws_ticket, require_scope, verify_ws_ticket
from src.api.errors import AppError
from src.api.models import ErrorDetail, TokenPayload, WsTicketResponse
from src.core.domain.events import Event
from src.core.ports.event_bus import EventBus
from src.deps import get_event_bus
from src.utils.config import (
    STREAM_HEARTBEAT_SECONDS,
    STREAM_MAX_DURATION_SECONDS,
    WS_MAX_MESSAGE_BYTES,
)

logger = logging.getLogger(__name__)

router = APIRouter()

TOPICS = ("items", "webhooks")
Topic = Literal["items", "webhooks"]


# ── SSE ────────────────────────────────────────────────────────────────────

def _sse_frame(event: Event) -> str:
    return f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(event.data)}\n\n"


async def _sse_body(
    bus: EventBus, topic: str, last_event_id: Optional[str], release
) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    reason = "client_disconnect"
    stream_metrics.stream_opened("sse")
    try:
        yield "retry: 3000\n\n"  # EventSource reconnect delay
        async for item in streaming.with_heartbeat(
            bus.subscribe(topic, last_event_id),
            STREAM_HEARTBEAT_SECONDS,
            STREAM_MAX_DURATION_SECONDS,
        ):
            if item is None:
                yield ": heartbeat\n\n"
            else:
                yield _sse_frame(item)
                stream_metrics.message_sent("sse")
        reason = (
            "max_duration"
            if loop.time() - started >= STREAM_MAX_DURATION_SECONDS - 0.5
            else "slow_consumer"
        )
    finally:
        release()
        stream_metrics.stream_closed("sse", reason)


@router.get(
    "/events/stream",
    tags=["Streaming"],
    summary="Server-Sent Events stream",
    description=(
        "Long-lived `text/event-stream`. Send `Last-Event-ID` to resume after a "
        "reconnect (best-effort replay). The server ends the stream every "
        f"{STREAM_MAX_DURATION_SECONDS}s; EventSource reconnects automatically."
    ),
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "Event stream"},
        503: {"model": ErrorDetail, "description": "Connection limit reached"},
    },
)
async def stream_events(
    topic: Topic = Query("items", description="Event topic"),
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    bus: EventBus = Depends(get_event_bus),
    current_user: TokenPayload = Depends(require_scope("events:read")),
):
    if not streaming.limiter.try_acquire():
        raise AppError(
            503,
            "STREAM_LIMIT_REACHED",
            "Too many open streams on this instance; retry shortly",
            headers={"Retry-After": "5"},
        )
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            streaming.limiter.release()

    return StreamingResponse(
        _sse_body(bus, topic, last_event_id, release),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
        # Covers the narrow window where the client vanishes before the body
        # generator ever starts (its own finally would never run).
        background=BackgroundTask(release),
    )


# ── WebSocket ──────────────────────────────────────────────────────────────

@router.post(
    "/ws/ticket",
    response_model=WsTicketResponse,
    tags=["Streaming"],
    summary="Get a WebSocket ticket",
    description=(
        "Browsers cannot send an Authorization header when opening a WebSocket. "
        "Exchange your Bearer JWT for a short-lived single-use ticket and connect "
        "to `/ws?ticket=<ticket>`."
    ),
)
async def issue_ws_ticket(
    current_user: TokenPayload = Depends(require_scope("events:read")),
):
    ticket, ttl = create_ws_ticket(current_user)
    return WsTicketResponse(ticket=ticket, expires_in=ttl)


async def _close(websocket: WebSocket, code: int, reason: str = "") -> None:
    with contextlib.suppress(RuntimeError):  # already closed by either side
        await websocket.close(code=code, reason=reason)


async def _pump(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    bus: EventBus,
    topic: str,
    last_event_id: Optional[str],
) -> tuple[int, str]:
    """Push bus events to the client until the stream ends. Returns the close code."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    async for item in streaming.with_heartbeat(
        bus.subscribe(topic, last_event_id),
        STREAM_HEARTBEAT_SECONDS,
        STREAM_MAX_DURATION_SECONDS,
    ):
        message = (
            {"type": "heartbeat"}
            if item is None
            else {"type": "event", "id": item.id, "event": item.type, "data": item.data}
        )
        async with send_lock:
            await websocket.send_text(json.dumps(message))
        if item is not None:
            stream_metrics.message_sent("websocket")
    if loop.time() - started >= STREAM_MAX_DURATION_SECONDS - 0.5:
        return 1001, "max duration reached; reconnect"
    return 1013, "slow consumer"


async def _listen(websocket: WebSocket, send_lock: asyncio.Lock) -> Optional[tuple[int, str]]:
    """
    Read client messages. Returns None when the client disconnects, or a
    (code, reason) when the server should close. Supported: {"type": "ping"}.
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        text = message.get("text")
        raw = text.encode() if text is not None else message.get("bytes") or b""
        if len(raw) > WS_MAX_MESSAGE_BYTES:
            return 1009, "message too large"
        reply: dict
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("type") == "ping":
                reply = {"type": "pong"}
            else:
                reply = {"type": "error", "code": "UNSUPPORTED_MESSAGE"}
        except ValueError:
            reply = {"type": "error", "code": "INVALID_JSON"}
        async with send_lock:
            await websocket.send_text(json.dumps(reply))


@router.websocket("/ws")
async def stream_ws(
    websocket: WebSocket,
    ticket: Optional[str] = Query(None),
    topic: str = Query("items"),
    last_event_id: Optional[str] = Query(None),
    bus: EventBus = Depends(get_event_bus),
):
    # Accept first so the client receives a meaningful close code; closing
    # before accept would surface only as an opaque handshake failure.
    await websocket.accept()

    if topic not in TOPICS:
        return await _close(websocket, 4400, "unknown topic")
    if not ticket:
        return await _close(websocket, 4401, "ticket required")
    try:
        user = verify_ws_ticket(ticket)
    except AppError:
        return await _close(websocket, 4401, "invalid ticket")
    if "events:read" not in user.scopes:
        return await _close(websocket, 4403, "missing scope events:read")
    if not streaming.limiter.try_acquire():
        return await _close(websocket, 1013, "too many open streams")

    stream_metrics.stream_opened("websocket")
    reason = "client_disconnect"
    send_lock = asyncio.Lock()
    sender = asyncio.create_task(_pump(websocket, send_lock, bus, topic, last_event_id))
    receiver = asyncio.create_task(_listen(websocket, send_lock))
    try:
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        close: Optional[tuple[int, str]] = None
        if sender.done() and not sender.cancelled() and sender.exception() is None:
            close = sender.result()
            reason = "max_duration" if close[0] == 1001 else "slow_consumer"
        elif receiver.done() and not receiver.cancelled() and receiver.exception() is None:
            close = receiver.result()
            if close is not None:
                reason = "message_too_large"
        for task in (sender, receiver):
            task.cancel()
        # asyncio.wait, not gather: gather re-raises a cancelled child's
        # CancelledError into this task, which would abort the endpoint.
        await asyncio.wait({sender, receiver})
        for task in (sender, receiver):
            if not task.cancelled() and task.exception() is not None:
                # Typically a send on a socket the client already closed.
                logger.debug("WebSocket task ended with %r", task.exception())
        if close is not None:
            await _close(websocket, *close)
    finally:
        streaming.limiter.release()
        stream_metrics.stream_closed("websocket", reason)
