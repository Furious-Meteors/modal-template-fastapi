"""
src/core/ports/ticket_store.py
==============================
Replay protection for WebSocket tickets. A ticket is a stateless signed JWT, so
"single use" needs somewhere to remember which ticket ids were already used.

consume() must be atomic: of two concurrent calls with the same jti exactly one
returns True. A shared backend (Postgres) makes this hold across containers; the
in-memory adapter only protects within one process.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TicketReplayStore(Protocol):
    async def consume(self, jti: str, expires_at: float) -> bool:
        """True the first time `jti` is seen (it is now consumed), False on replay.

        `expires_at` is unix seconds; the entry may be discarded after it.
        """
        ...
