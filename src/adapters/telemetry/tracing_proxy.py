"""
src/adapters/telemetry/tracing_proxy.py
=========================================
Generic telemetry sidecar for any async adapter.

Wraps any repository (or service) and intercepts every async method call,
creating a child OTel span around it. Neither the real adapter nor the service
layer need to know telemetry exists.

Usage in deps.py:
    from src.adapters.telemetry.tracing_proxy import TracingProxy

    raw = InMemoryItemRepository()
    return TracingProxy(raw, prefix="repository.item")

    # Works unchanged for any backend:
    raw = PostgresItemRepository(os.environ["DATABASE_URL"])
    return TracingProxy(raw, prefix="repository.item")

Span naming:
    prefix + "." + method_name
    e.g. "repository.item.get", "repository.item.create"

Span attributes:
    Method-level only (name, prefix). For richer attribute sets
    (item IDs, result counts, cache hits), graduate to an explicit
    typed wrapper for that specific port.

Performance:
    First call per method: __getattr__ lookup + closure allocation (~300ns)
    Subsequent calls:      direct attribute access on cached closure (~50ns)
    Span creation:         ~1-3µs (OTel SDK, non-blocking)
    Export:                zero — BatchSpanProcessor flushes in background thread
"""
from __future__ import annotations

import asyncio

from src.infrastructure.setup import get_tracer


class TracingProxy:
    """
    Generic async telemetry sidecar. Thread-safe and reusable across requests.
    Wraps any object whose async methods should be traced as child spans.
    """

    def __init__(self, wrapped: object, prefix: str) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_prefix", prefix)

    def __getattr__(self, name: str):
        method = getattr(object.__getattribute__(self, "_wrapped"), name)

        if not asyncio.iscoroutinefunction(method):
            return method

        prefix = object.__getattribute__(self, "_prefix")

        async def _traced(*args, **kwargs):
            with get_tracer().start_as_current_span(f"{prefix}.{name}"):
                return await method(*args, **kwargs)

        # Cache on the instance so subsequent calls bypass __getattr__ entirely.
        object.__setattr__(self, name, _traced)
        return _traced
