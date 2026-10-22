"""
src/adapters/persistence/memory.py
====================================
In-memory persistence adapter — ships with the template.

DEVELOPMENT AND TESTS ONLY. Each process/container holds its own private store,
so under Modal autoscaling requests can hit containers with different data, and
everything is lost on scale-down. Not suitable for multi-container deployments.

Satisfies ItemRepository Protocol structurally (no inheritance needed).
Replace or supplement with a concrete DB adapter in src/deps.py.

Thread-safety note: sufficient for Modal's single-threaded async workers.
For multi-threaded environments, protect _store with asyncio.Lock.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from src.core.domain.errors import DuplicateItemNameError
from src.core.domain.item import Item


class InMemoryItemRepository:
    def __init__(self) -> None:
        self._store: Dict[str, Item] = {}

    async def get(self, item_id: str) -> Optional[Item]:
        return self._store.get(item_id)

    async def get_by_name(self, name: str) -> Optional[Item]:
        wanted = name.strip().casefold()
        return next(
            (i for i in self._store.values() if i.name.strip().casefold() == wanted),
            None,
        )

    async def list(self, limit: int = 100, offset: int = 0) -> Tuple[List[Item], int]:
        all_items = list(self._store.values())
        total = len(all_items)
        return all_items[offset: offset + limit], total

    def _name_taken(self, name: str, *, by_other_than: Optional[str] = None) -> bool:
        wanted = name.strip().casefold()
        return any(
            i.id != by_other_than and i.name.strip().casefold() == wanted
            for i in self._store.values()
        )

    async def create(self, item: Item) -> Item:
        # The port requires adapters to enforce name uniqueness (a database does it with a unique
        # index). No awaits between the check and the write, so this is atomic on one event loop.
        if self._name_taken(item.name):
            raise DuplicateItemNameError(item.name)
        self._store[item.id] = item
        return item

    async def update(self, item: Item) -> Optional[Item]:
        if item.id not in self._store:
            return None
        if self._name_taken(item.name, by_other_than=item.id):  # keeping your own name is fine
            raise DuplicateItemNameError(item.name)
        self._store[item.id] = item
        return item

    async def delete(self, item_id: str) -> bool:
        return self._store.pop(item_id, None) is not None
