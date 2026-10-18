"""
src/core/ports/event_bus.py
===========================
Publish/subscribe port used by the streaming endpoints (SSE, WebSocket) and by
anything that wants to announce a domain event.

Adding a real backend (Modal Queue/Dict, Redis, NATS, ...):
  1. Create src/adapters/events/<your_backend>.py implementing these two methods.
  2. Add a branch in src/deps.py keyed on EVENT_BUS_BACKEND (a commented stub is there).
  3. Provide the same guarantees the in-memory bus documents below.

Contract an implementation must honour:
  • publish() assigns Event.id (monotonic, comparable as int) and Event.ts.
  • subscribe() yields events published AFTER the call, in order. When
    last_event_id is given it first replays any retained events newer than it
    (best-effort — retention is bounded and may not cover a long outage).
  • A subscriber that cannot keep up is disconnected (the iterator ends)
    rather than blocking publishers or growing memory without bound.

Multi-container note: the in-memory bus is per-process. With more than one
Modal container a stream on container A will not see events published on B. Use
a shared backend before scaling out.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional, Protocol, runtime_checkable

from src.core.domain.events import Event


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, topic: str, event_type: str, data: Dict[str, Any]) -> Event: ...

    def subscribe(
        self, topic: str, last_event_id: Optional[str] = None
    ) -> AsyncIterator[Event]: ...
