"""
src/core/domain/item.py
=======================
Pure domain entity. No I/O imports — no FastAPI, no DB drivers, no HTTP.

Consumers replace this with their own domain model. Keep to_dict() in sync
with whatever fields you add so the HTTP layer can serialise without knowing
the entity internals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Item:
    id: str
    name: str
    description: Optional[str] = None
    project_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "project_id": self.project_id,
        }
