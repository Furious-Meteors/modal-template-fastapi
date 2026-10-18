"""
src/api/streaming.py
====================
Helpers shared by the SSE and WebSocket endpoints: a per-container connection
cap and an iterator wrapper that adds heartbeats and a maximum lifetime.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator, Optional, TypeVar

from src.utils.config import STREAM_MAX_CONNECTIONS

T = TypeVar("T")


class StreamLimiter:
    """
    Caps concurrent long-lived connections. Every open stream also occupies one
    of Modal's max_concurrent_requests slots, so the cap must stay below it
    (modal_common enforces that) or streams could starve normal requests.
    """

    def __init__(self, max_connections: int) -> None:
        self.max_connections = max_connections
        self.active = 0

    def try_acquire(self) -> bool:
        if self.active >= self.max_connections:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        if self.active > 0:
            self.active -= 1


limiter = StreamLimiter(STREAM_MAX_CONNECTIONS)


async def with_heartbeat(
    source: AsyncIterator[T],
    interval: float,
    max_duration: Optional[float] = None,
) -> AsyncIterator[Optional[T]]:
    """
    Yield items from `source`, or None whenever `interval` seconds pass without
    one (callers turn None into a keep-alive). Ends when the source ends or
    `max_duration` elapses.

    A single __anext__ task is kept alive across heartbeats: cancelling an
    async generator's pending __anext__ (as asyncio.wait_for would) would
    terminate the generator.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_duration if max_duration else None
    pending: Optional[asyncio.Future] = None
    try:
        while True:
            timeout = interval
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return
                timeout = min(interval, remaining)
            if pending is None:
                pending = asyncio.ensure_future(source.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            if not done:
                if deadline is not None and loop.time() >= deadline:
                    return
                yield None
                continue
            finished, pending = pending, None
            try:
                item = finished.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
