"""
src/adapters/events/memory.py
=============================
In-memory event bus and webhook idempotency store — ship with the template.

DEVELOPMENT AND TESTS ONLY (see src/core/ports/event_bus.py): state is private
to the process, so under Modal autoscaling each container has its own bus.

Single event loop only; no locking is needed because publish() and the
register-and-snapshot step in subscribe() contain no awaits.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import Any, AsyncIterator, Callable, Deque, Dict, Optional, Set

from src.core.domain.events import Event

_CLOSE = object()


class _Subscriber:
    __slots__ = ("queue",)

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)


class InMemoryEventBus:
    def __init__(self, replay_size: int = 100, subscriber_queue_size: int = 64) -> None:
        self._replay_size = replay_size
        self._queue_size = subscriber_queue_size
        self._seq = 0
        self._history: Dict[str, Deque[Event]] = {}
        self._subscribers: Dict[str, Set[_Subscriber]] = {}

    async def publish(self, topic: str, event_type: str, data: Dict[str, Any]) -> Event:
        self._seq += 1
        event = Event(id=str(self._seq), type=event_type, data=data, ts=time.time())
        self._history.setdefault(topic, deque(maxlen=self._replay_size)).append(event)
        for sub in list(self._subscribers.get(topic, ())):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop its backlog and end its stream. It can
                # reconnect with Last-Event-ID and resume from the replay buffer.
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait(_CLOSE)
                self._subscribers[topic].discard(sub)
        return event

    async def subscribe(
        self, topic: str, last_event_id: Optional[str] = None
    ) -> AsyncIterator[Event]:
        sub = _Subscriber(self._queue_size)
        # Register and snapshot with no await in between, so nothing is missed
        # or delivered twice across the replay/live boundary.
        self._subscribers.setdefault(topic, set()).add(sub)
        replay = self._replay_after(topic, last_event_id)
        try:
            for event in replay:
                yield event
            while True:
                item = await sub.queue.get()
                if item is _CLOSE:
                    return
                yield item
        finally:
            self._subscribers.get(topic, set()).discard(sub)

    def _replay_after(self, topic: str, last_event_id: Optional[str]) -> list[Event]:
        if last_event_id is None:
            return []
        try:
            last = int(last_event_id)
        except ValueError:
            return []
        return [e for e in self._history.get(topic, ()) if int(e.id) > last]


class InMemoryWebhookStore:
    def __init__(
        self,
        ttl_seconds: float = 86_400,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        # key -> expiry. Fixed TTL means insertion order is also expiry order.
        self._seen: "OrderedDict[tuple[str, str], float]" = OrderedDict()

    def _purge(self) -> None:
        now = self._clock()
        while self._seen:
            key, expiry = next(iter(self._seen.items()))
            if expiry > now and len(self._seen) <= self._max:
                break
            del self._seen[key]

    async def record_if_new(self, provider: str, webhook_id: str) -> bool:
        self._purge()
        key = (provider, webhook_id)
        if key in self._seen:
            return False
        self._seen[key] = self._clock() + self._ttl
        return True

    async def forget(self, provider: str, webhook_id: str) -> None:
        self._seen.pop((provider, webhook_id), None)
