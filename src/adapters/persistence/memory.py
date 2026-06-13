"""
src/adapters/persistence/memory.py
====================================
In-memory persistence adapter — ships with the template.

Satisfies ItemRepository Protocol structurally (no inheritance needed).
Replace or supplement with a concrete DB adapter in src/deps.py.

Thread-safety note: sufficient for Modal's single-threaded async workers.
For multi-threaded environments, protect _store with asyncio.Lock.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from src.core.domain.item import Item


class InMemoryItemRepository:
    def __init__(self) -> None:
        self._store: Dict[str, Item] = {}

    async def get(self, item_id: str) -> Optional[Item]:
        return self._store.get(item_id)

    async def list(self, limit: int = 100, offset: int = 0) -> Tuple[List[Item], int]:
        all_items = list(self._store.values())
        total = len(all_items)
        return all_items[offset: offset + limit], total

    async def create(self, item: Item) -> Item:
        self._store[item.id] = item
        return item

    async def update(self, item: Item) -> Optional[Item]:
        if item.id not in self._store:
            return None
        self._store[item.id] = item
        return item

    async def delete(self, item_id: str) -> bool:
        return self._store.pop(item_id, None) is not None
