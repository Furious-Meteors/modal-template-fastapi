"""
src/core/domain/events.py
=========================
Pure domain event. No I/O imports — the bus adapter assigns `id` and `ts` when
an event is published, so producers only supply a type and a payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class Event:
    id: str                 # monotonically increasing per bus; used as SSE Last-Event-ID
    type: str               # e.g. "item.created", "webhook.github"
    data: Dict[str, Any]
    ts: float               # unix seconds, set by the bus at publish time

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "type": self.type, "data": self.data, "ts": self.ts}
