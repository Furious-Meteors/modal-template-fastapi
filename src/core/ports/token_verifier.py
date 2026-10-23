"""
src/core/ports/token_verifier.py
================================
How the API checks a bearer token. The adapter owns everything about *who issued it*
(a shared secret, an identity provider's published keys, ...); the API layer only sees
validated claims.

verify() must raise TokenExpiredError / InvalidTokenError — never return claims it did not
validate. Required claims: `exp` and `sub`.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenVerifier(Protocol):
    def verify(self, token: str) -> dict:
        """Return the token's claims, or raise TokenExpiredError / InvalidTokenError."""
        ...
