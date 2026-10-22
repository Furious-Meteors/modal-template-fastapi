"""
src/api/event_access.py
=======================
Who may see which events on the streaming endpoints.

Two layers:

1. Topic scopes — a stream must not reveal what the REST API would deny, so
   subscribing to a topic needs that topic's scope in addition to `events:read`:

       items     → items:read      (same as GET /items)
       webhooks  → webhooks:read   (third-party payloads: grant deliberately)

2. can_view() — a per-event hook, called for every event and every subscriber.
   The default allows everything, which is right for the template's shared item
   catalogue. Replace it when events belong to someone, e.g.

       def can_view(event, user):
           return event.data.get("owner") == user.sub or "admin" in user.scopes

   It runs on the hot path, so keep it synchronous and cheap.
"""
from __future__ import annotations

from src.api.errors import AppError
from src.api.models import TokenPayload
from src.core.domain.events import Event

TOPIC_SCOPES: dict[str, str] = {
    "items": "items:read",
    "webhooks": "webhooks:read",
}

# Scopes a WebSocket ticket may carry: exactly what stream access can need.
TICKET_SCOPES = frozenset({"events:read", *TOPIC_SCOPES.values()})


def required_topic_scope(topic: str) -> str:
    return TOPIC_SCOPES[topic]


def ensure_topic_access(topic: str, user: TokenPayload) -> None:
    """Raise 403 unless `user` may subscribe to `topic` (events:read is checked separately)."""
    needed = required_topic_scope(topic)
    if needed not in user.scopes:
        raise AppError(403, "INSUFFICIENT_SCOPE", f"Missing required scopes: {needed}")


def can_view(event: Event, user: TokenPayload) -> bool:
    """Per-event visibility hook. Default: every subscriber sees every event on a topic they may read."""
    return True
