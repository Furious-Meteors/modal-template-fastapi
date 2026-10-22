"""
src/adapters/telemetry/metered.py
=================================
Business-metrics sidecars. Like TracingProxy they wrap an object from the
outside, so ItemService, the event bus and the routes contain no telemetry code.

  MeteredItemService   outcome + duration of every service operation
  MeteredEventBus      publish outcome/duration, and delivery lag to consumers

Wired in src/deps.py. Instruments live in src/infrastructure/metrics.py.
"""
from __future__ import annotations

import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

from src.core.domain.errors import DuplicateItemNameError, StorageUnavailableError
from src.core.domain.events import Event
from src.infrastructure import metrics as business_metrics
from src.infrastructure.metrics import BusinessMetrics

# An event older than this when its subscription began is a replay, not a live
# event. The margin absorbs ordinary clock differences between hosts.
_REPLAY_MARGIN_SECONDS = 2.0


class MeteredItemService:
    """Wraps an ItemService; same interface, plus metrics."""

    def __init__(self, inner: Any, metrics: Optional[BusinessMetrics] = None) -> None:
        self._inner = inner
        self._metrics = metrics

    def _m(self) -> BusinessMetrics:
        return self._metrics or business_metrics.get()

    async def _observe(
        self,
        operation: str,
        call: Awaitable[Any],
        is_missing: Callable[[Any], bool] = lambda result: False,
    ) -> Any:
        started = time.perf_counter()
        outcome = "ok"
        try:
            result = await call
            if is_missing(result):
                outcome = "not_found"
            return result
        except DuplicateItemNameError:
            outcome = "conflict"
            raise
        except StorageUnavailableError:
            outcome = "unavailable"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            self._m().record_item_op(operation, outcome, (time.perf_counter() - started) * 1000)

    async def create(self, *args, **kwargs):
        return await self._observe("create", self._inner.create(*args, **kwargs))

    async def get(self, *args, **kwargs):
        return await self._observe("get", self._inner.get(*args, **kwargs), lambda r: r is None)

    async def list(self, *args, **kwargs):
        return await self._observe("list", self._inner.list(*args, **kwargs))

    async def update(self, *args, **kwargs):
        return await self._observe("update", self._inner.update(*args, **kwargs), lambda r: r is None)

    async def delete(self, *args, **kwargs):
        return await self._observe("delete", self._inner.delete(*args, **kwargs), lambda r: not r)


class MeteredEventBus:
    """Wraps an EventBus; same interface, plus metrics. Other attributes pass through."""

    def __init__(self, inner: Any, metrics: Optional[BusinessMetrics] = None) -> None:
        self._inner = inner
        self._metrics = metrics

    def _m(self) -> BusinessMetrics:
        return self._metrics or business_metrics.get()

    def __getattr__(self, name: str):  # start(), close(), subscriber_count(), ...
        return getattr(self._inner, name)

    async def publish(self, topic: str, event_type: str, data: Dict[str, Any]) -> Event:
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await self._inner.publish(topic, event_type, data)
        except Exception:
            outcome = "error"
            raise
        finally:
            self._m().record_event_published(
                topic, event_type, outcome, (time.perf_counter() - started) * 1000
            )

    def subscribe(self, topic: str, last_event_id: Optional[str] = None) -> AsyncIterator[Event]:
        return self._subscribe(topic, last_event_id)

    async def _subscribe(self, topic: str, last_event_id: Optional[str]) -> AsyncIterator[Event]:
        started = time.time()
        inner = self._inner.subscribe(topic, last_event_id)
        try:
            async for event in inner:
                replayed = event.ts < started - _REPLAY_MARGIN_SECONDS
                self._m().record_event_delivered(topic, (time.time() - event.ts) * 1000, replayed)
                yield event
            # Falling out of the loop means the bus ended the stream itself: a
            # consumer that stops early closes this generator instead (GeneratorExit
            # or cancellation skips this line). That is how a slow consumer shows up.
            self._m().record_subscription_dropped(topic)
        finally:
            aclose = getattr(inner, "aclose", None)
            if aclose is not None:
                await aclose()
