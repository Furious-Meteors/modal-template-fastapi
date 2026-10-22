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

Name uniqueness: adapters MUST enforce it themselves, comparing names ignoring case and
surrounding whitespace, and raise DuplicateItemNameError from create() and from update()
when the name belongs to a DIFFERENT item (an item may keep its own name). A database does
this with a unique index on the normalised name, which is also the only race-free way; the
service no longer checks first, which saved a round trip per write. update() returns None
for a missing item. get_by_name() remains for lookups. tests/test_item_repository_contract.py
holds every adapter to this.
"""
from __future__ import annotations

from typing import List, Optional, Protocol, Tuple, runtime_checkable

from src.core.domain.item import Item


@runtime_checkable
class ItemRepository(Protocol):
    async def get(self, item_id: str) -> Optional[Item]: ...
    async def get_by_name(self, name: str) -> Optional[Item]:
        """Return the item whose name matches, ignoring case and surrounding whitespace."""
        ...
    async def list(self, limit: int, offset: int) -> Tuple[List[Item], int]: ...
    async def create(self, item: Item) -> Item: ...
    async def update(self, item: Item) -> Optional[Item]: ...
    async def delete(self, item_id: str) -> bool: ...
