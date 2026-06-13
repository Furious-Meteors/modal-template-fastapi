"""
src/core/ports/item_repository.py
==================================
Abstract persistence port — the contract the business layer needs from storage.

Uses Protocol (structural subtyping) so adapters don't need to inherit from
anything. Any object with the right async method signatures satisfies this port.

Adding a new persistence backend:
  1. Create src/adapters/persistence/<your_backend>.py
  2. Implement a class with these methods (no import of this file needed)
  3. Wire it in src/deps.py
"""
from __future__ import annotations

from typing import List, Optional, Protocol, Tuple, runtime_checkable

from src.core.domain.item import Item


@runtime_checkable
class ItemRepository(Protocol):
    async def get(self, item_id: str) -> Optional[Item]: ...
    async def list(self, limit: int, offset: int) -> Tuple[List[Item], int]: ...
    async def create(self, item: Item) -> Item: ...
    async def update(self, item: Item) -> Optional[Item]: ...
    async def delete(self, item_id: str) -> bool: ...
