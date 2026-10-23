"""
src/adapters/auth/jwt.py
========================
TokenVerifier adapters (PyJWT). Same checks either way; only where the key comes from differs:

  shared_secret_verifier  one HS256/384/512 secret you control            (AUTH_BACKEND=secret)
  jwks_verifier           the identity provider's published public keys   (AUTH_BACKEND=jwks)
                          — Auth0, Cognito, Clerk, Keycloak, Supabase Auth, ... Keys are fetched
                          from the JWKS URL, cached, and re-fetched when a token names an unknown
                          `kid`, so provider key rotation needs no redeploy.

Asymmetric algorithms need the `cryptography` package (pinned in modal_common.PIP_PACKAGES).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Callable, Optional, Sequence

import jwt

from src.core.domain.errors import InvalidTokenError, TokenExpiredError

logger = logging.getLogger(__name__)

SYMMETRIC_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})
ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)


class JwtVerifier:
    def __init__(
        self,
        key_for: Callable[[str], object],
        algorithms: Sequence[str],
        audience: Optional[str] = None,
        issuer: Optional[str] = None,
    ) -> None:
        self._key_for = key_for
        self._algorithms = list(algorithms)
        self._extra = {k: v for k, v in (("audience", audience), ("issuer", issuer)) if v}

    def verify(self, token: str) -> dict:
        try:
            return jwt.decode(
                token,
                self._key_for(token),
                algorithms=self._algorithms,
                options={"require": ["exp", "sub"]},
                **self._extra,
            )
        except jwt.ExpiredSignatureError:
            raise TokenExpiredError() from None
        except jwt.PyJWKClientError:
            # Unknown `kid`, or the key endpoint is unreachable (cached keys keep working).
            logger.warning("Could not resolve a signing key for the token", exc_info=True)
            raise InvalidTokenError() from None
        except jwt.InvalidTokenError:
            raise InvalidTokenError() from None


def shared_secret_verifier(secret: str, algorithm: str, audience=None, issuer=None) -> JwtVerifier:
    return JwtVerifier(lambda _token: secret, [algorithm], audience, issuer)


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> "jwt.PyJWKClient":
    # One client per URL for the life of the process: it owns the key cache.
    return jwt.PyJWKClient(url, cache_keys=True, timeout=5)


def jwks_verifier(url: str, algorithms: Sequence[str], audience=None, issuer=None) -> JwtVerifier:
    client = _jwks_client(url)
    return JwtVerifier(lambda token: client.get_signing_key_from_jwt(token).key, algorithms, audience, issuer)
