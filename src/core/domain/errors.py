"""
src/core/domain/errors.py
=========================
Domain-level errors. Pure Python — the HTTP layer maps these to status codes.
"""
from __future__ import annotations


class DuplicateItemNameError(Exception):
    """Raised when creating or renaming an item to a name another item already uses."""

    def __init__(self, name: str) -> None:
        super().__init__(f"An item named '{name}' already exists")
        self.name = name
