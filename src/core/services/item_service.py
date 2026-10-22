"""
src/core/services/item_service.py
==================================
Business logic for items. Depends only on the ItemRepository port —
no knowledge of HTTP, DB drivers, or infrastructure.

Consumers extend this class (or add new service classes) for their domain.
Telemetry is handled transparently by TelemetryMiddleware at the HTTP layer;
add get_tracer() spans here only for fine-grained sub-request tracing.
"""
from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Tuple

from src.core.domain.item import Item
from src.core.ports.event_bus import EventBus
from src.core.ports.item_repository import ItemRepository

logger = logging.getLogger(__name__)

ITEMS_TOPIC = "items"


class ItemService:
    def __init__(self, repo: ItemRepository, events: Optional[EventBus] = None) -> None:
        self._repo = repo
        self._events = events

    async def _emit(self, event_type: str, data: dict) -> None:
        """Announce a change. Best-effort: a bus failure must never fail the write."""
        if self._events is None:
            return
        try:
            await self._events.publish(ITEMS_TOPIC, event_type, data)
        except Exception:
            logger.exception("Failed to publish %s", event_type)

    # Name uniqueness is enforced by the repository, which raises DuplicateItemNameError; it
    # propagates to the 409 handler as-is. See src/core/ports/item_repository.py.

    async def create(
        self,
        name: str,
        description: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Item:
        # Uniqueness is the repository's job (it raises DuplicateItemNameError): checking here first
        # would cost an extra round trip and still leave a race the database has to close anyway.
        item = Item(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            project_id=project_id,
        )
        created = await self._repo.create(item)
        await self._emit("item.created", created.to_dict())
        return created

    async def get(self, item_id: str) -> Optional[Item]:
        return await self._repo.get(item_id)

    async def list(self, limit: int = 100, offset: int = 0) -> Tuple[List[Item], int]:
        return await self._repo.list(limit=limit, offset=offset)

    async def update(
        self,
        item_id: str,
        name: str,
        description: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Optional[Item]:
        # One write: the repository returns None for a missing item and raises
        # DuplicateItemNameError if the name belongs to a different item (keeping your own is fine).
        updated = Item(id=item_id, name=name, description=description, project_id=project_id)
        result = await self._repo.update(updated)
        if result is not None:
            await self._emit("item.updated", result.to_dict())
        return result

    async def delete(self, item_id: str) -> bool:
        deleted = await self._repo.delete(item_id)
        if deleted:
            await self._emit("item.deleted", {"id": item_id})
        return deleted
