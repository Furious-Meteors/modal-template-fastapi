"""
src/api/webhooks.py
===================
Inbound webhooks with a generic HMAC-SHA256 scheme.

    POST /webhooks/{provider}
    X-Webhook-Id:        unique delivery id (idempotency key)
    X-Webhook-Timestamp: unix seconds
    X-Webhook-Signature: sha256=<hex hmac of "<timestamp>.<raw body>">

The secret for provider "acme" is the env var WEBHOOK_SECRET_ACME (set it in a
Modal secret). Order of checks — nothing is parsed or stored until the
signature verifies, so unauthenticated callers cannot burn idempotency storage:

    known provider → body size (enforced while reading) → timestamp fresh → signature → webhook id →
    JSON object body → duplicate? → publish → 202

The handler only validates and publishes to the "webhooks" topic, then returns
202; do real work in a consumer of that topic so providers never time out.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time

from fastapi import APIRouter, Depends, Request, Response, status

from src.api import ratelimit
from src.api.errors import AppError, get_session_id
from src.api.models import ErrorDetail, WebhookResponse
from src.core.ports.event_bus import EventBus
from src.core.ports.webhook_store import WebhookEventStore
from src.deps import get_event_bus, get_webhook_store
from src.utils.config import WEBHOOK_MAX_BODY_BYTES, WEBHOOK_TOLERANCE_SECONDS

logger = logging.getLogger(__name__)

router = APIRouter()

WEBHOOKS_TOPIC = "webhooks"
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_WEBHOOK_ID_RE = re.compile(r"^[\x21-\x7e]{1,128}$")  # printable, no spaces


def _secret_for(provider: str) -> bytes | None:
    if not _PROVIDER_RE.match(provider):
        return None
    secret = os.environ.get(f"WEBHOOK_SECRET_{provider.upper().replace('-', '_')}")
    return secret.encode() if secret else None


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    """The signature a sender must produce. Exposed for tests and sender docs."""
    mac = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def _read_limited(request: Request, limit: int) -> bytes | None:
    """The request body, or None as soon as it exceeds `limit` bytes. Memory is bounded by limit + one chunk."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/webhooks/{provider}",
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Webhooks"],
    summary="Receive a signed webhook",
    responses={
        200: {"model": WebhookResponse, "description": "Duplicate delivery (already accepted)"},
        400: {"model": ErrorDetail, "description": "Missing/stale timestamp, missing id, bad payload"},
        401: {"model": ErrorDetail, "description": "Missing or invalid signature"},
        404: {"model": ErrorDetail, "description": "Unknown provider"},
        429: {"model": ErrorDetail, "description": "Rate limited (retry after the given delay)"},
        413: {"model": ErrorDetail, "description": "Body too large"},
    },
)
async def receive_webhook(
    provider: str,
    request: Request,
    response: Response,
    bus: EventBus = Depends(get_event_bus),
    store: WebhookEventStore = Depends(get_webhook_store),
    session_id: str = Depends(get_session_id),
):
    secret = _secret_for(provider)
    if secret is None:
        # Unknown and unconfigured providers look identical; nothing is recorded
        # as a metric label here, keeping cardinality bounded.
        raise AppError(404, "UNKNOWN_WEBHOOK_PROVIDER", "Unknown webhook provider")

    # Per provider (never per IP): generous enough that real traffic never hits
    # it, and providers retry a 429. Checked before any body is read.
    wait = await ratelimit.webhook_limiter.acquire(provider)
    if wait:
        raise AppError(
            429, "RATE_LIMITED", "Too many webhook deliveries; retry shortly",
            headers=ratelimit.TokenBucketLimiter.retry_after_header(wait),
        )

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > WEBHOOK_MAX_BODY_BYTES:
        raise AppError(413, "PAYLOAD_TOO_LARGE", "Webhook body too large")
    # Content-Length can be absent (chunked upload) or a lie, so the limit is enforced while
    # reading: we stop at the first chunk that crosses it instead of buffering the whole body.
    body = await _read_limited(request, WEBHOOK_MAX_BODY_BYTES)
    if body is None:
        raise AppError(413, "PAYLOAD_TOO_LARGE", "Webhook body too large")

    timestamp = request.headers.get("x-webhook-timestamp")
    if not timestamp:
        raise AppError(400, "MISSING_TIMESTAMP", "X-Webhook-Timestamp header required")
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise AppError(400, "INVALID_TIMESTAMP", "X-Webhook-Timestamp must be unix seconds")
    if abs(time.time() - sent_at) > WEBHOOK_TOLERANCE_SECONDS:
        raise AppError(400, "STALE_TIMESTAMP", "Webhook timestamp outside the allowed window")

    provided = request.headers.get("x-webhook-signature")
    if not provided:
        raise AppError(401, "MISSING_SIGNATURE", "X-Webhook-Signature header required")
    if not hmac.compare_digest(provided.encode(), sign(secret, timestamp, body).encode()):
        raise AppError(401, "INVALID_SIGNATURE", "Invalid webhook signature")

    # ── Authenticated from here on ───────────────────────────────────────
    webhook_id = request.headers.get("x-webhook-id", "")
    if not _WEBHOOK_ID_RE.match(webhook_id):
        raise AppError(400, "MISSING_WEBHOOK_ID", "X-Webhook-Id header required (1-128 printable chars)")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise AppError(400, "INVALID_PAYLOAD", "Body must be a JSON object")

    if not await store.record_if_new(provider, webhook_id):
        response.status_code = status.HTTP_200_OK
        return WebhookResponse(
            session_id=session_id, status="duplicate", message="Webhook already received"
        )

    try:
        await bus.publish(
            WEBHOOKS_TOPIC,
            f"webhook.{provider}",
            {"webhook_id": webhook_id, "provider": provider, "payload": payload},
        )
    except Exception:
        # Let the provider retry: forget the id so the retry is not a "duplicate".
        await store.forget(provider, webhook_id)
        raise
    return WebhookResponse(
        session_id=session_id, status="accepted", message="Webhook accepted"
    )
