"""
src/core/ports/webhook_store.py
===============================
Idempotency store for inbound webhooks. Providers retry deliveries, so the
handler records each (provider, webhook id) and treats repeats as duplicates.

record_if_new() must be atomic: of two concurrent calls with the same key,
exactly one returns True. Real backends should use a unique key / SET NX with a
TTL. The in-memory adapter is per-process, so duplicates delivered to different
containers are NOT detected — use a shared backend when scaling out.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WebhookEventStore(Protocol):
    async def record_if_new(self, provider: str, webhook_id: str) -> bool:
        """True if this is the first time the key is seen (and it is now recorded)."""
        ...

    async def forget(self, provider: str, webhook_id: str) -> None:
        """Undo record_if_new so a failed delivery can be retried by the provider."""
        ...
