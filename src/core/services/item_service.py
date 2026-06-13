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

import uuid
from typing import List, Optional, Tuple

from src.core.domain.item import Item
from src.core.ports.item_repository import ItemRepository


class ItemService:
    def __init__(self, repo: ItemRepository) -> None:
        self._repo = repo

    async def create(
        self,
        name: str,
        description: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Item:
        item = Item(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            project_id=project_id,
        )
        return await self._repo.create(item)

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
        existing = await self._repo.get(item_id)
        if existing is None:
            return None
        updated = Item(id=item_id, name=name, description=description, project_id=project_id)
        return await self._repo.update(updated)

    async def delete(self, item_id: str) -> bool:
        return await self._repo.delete(item_id)
