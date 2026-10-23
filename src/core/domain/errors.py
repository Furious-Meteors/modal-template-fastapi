"""
src/core/domain/errors.py
=========================
Domain-level errors. Pure Python — the HTTP layer maps these to status codes.
"""
from __future__ import annotations


class StorageUnavailableError(Exception):
    """The persistence backend cannot be reached right now (outage, restart, pool exhausted).

    Distinct from a bug: callers can retry later. The HTTP layer maps it to 503.
    """


class DuplicateItemNameError(Exception):
    """Raised when creating or renaming an item to a name another item already uses."""

    def __init__(self, name: str) -> None:
        super().__init__(f"An item named '{name}' already exists")
        self.name = name


class TokenExpiredError(Exception):
    """The bearer token was valid but has expired."""


class InvalidTokenError(Exception):
    """The bearer token is malformed, badly signed, or fails an audience/issuer/claim check."""
