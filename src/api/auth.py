import logging
import os
import time
import uuid

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from src.adapters.auth.jwt import SYMMETRIC_ALGORITHMS, ASYMMETRIC_ALGORITHMS
from src.api.errors import AppError
from src.api.models import TokenPayload
from src.core.domain.errors import InvalidTokenError, TokenExpiredError
from src.deps import auth_backend, get_token_verifier
from src.utils.config import WS_TICKET_TTL_SECONDS

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

MIN_SECRET_LENGTH = 32
ALLOWED_ALGORITHMS = SYMMETRIC_ALGORITHMS  # shared-secret mode; JWKS mode takes ASYMMETRIC_ALGORITHMS


def _get_secret_key() -> str:
    key = os.environ.get("JWT_SECRET")
    if not key:
        raise RuntimeError("JWT_SECRET environment variable is not set")
    return key


def _get_algorithm() -> str:
    return os.environ.get("JWT_ALGORITHM", "HS256")


def _ticket_secret() -> str:
    """
    Key that signs and checks WebSocket tickets. Tickets are issued and consumed by this
    service, so they never depend on the identity provider: with AUTH_BACKEND=jwks set
    WS_TICKET_SECRET; with a shared secret it defaults to JWT_SECRET.
    """
    key = os.environ.get("WS_TICKET_SECRET") or os.environ.get("JWT_SECRET")
    if not key:
        raise RuntimeError("WS_TICKET_SECRET (or JWT_SECRET) must be set to sign WebSocket tickets")
    return key


def _ticket_algorithm() -> str:
    algorithm = _get_algorithm()
    return algorithm if algorithm in SYMMETRIC_ALGORITHMS else "HS256"


def _check_secret(name: str, value: str) -> None:
    if len(value) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"{name} must be at least {MIN_SECRET_LENGTH} characters long (got {len(value)})")


def validate_auth_config() -> None:
    """
    Fail-fast check for the auth environment. Called from the app lifespan so a missing or
    weak secret stops the container at startup instead of surfacing as a 500 on the first
    authenticated request.
    """
    backend = auth_backend()
    if backend == "secret":
        key = _get_secret_key()  # raises RuntimeError when missing/empty
        algorithm = _get_algorithm()
        if algorithm not in SYMMETRIC_ALGORITHMS:
            raise RuntimeError(
                f"JWT_ALGORITHM='{algorithm}' is not supported; "
                f"use one of: {', '.join(sorted(SYMMETRIC_ALGORITHMS))}"
            )
        _check_secret("JWT_SECRET", key)
    elif backend == "jwks":
        url = os.environ.get("JWT_JWKS_URL", "")
        if not url:
            raise RuntimeError("AUTH_BACKEND=jwks requires JWT_JWKS_URL")
        if not (url.startswith("https://") or url.startswith(("http://localhost", "http://127.0.0.1"))):
            raise RuntimeError("JWT_JWKS_URL must be an https:// URL (http is allowed only for localhost)")
        bad = [a for a in os.environ.get("JWT_ALGORITHM", "RS256").split(",") if a.strip() not in ASYMMETRIC_ALGORITHMS]
        if bad:
            raise RuntimeError(
                f"JWT_ALGORITHM={bad} is not supported with AUTH_BACKEND=jwks; "
                f"use one or more of: {', '.join(sorted(ASYMMETRIC_ALGORITHMS))}"
            )
        _check_secret("WS_TICKET_SECRET", os.environ.get("WS_TICKET_SECRET", ""))
        if not os.environ.get("JWT_AUDIENCE"):
            logger.warning(
                "AUTH_BACKEND=jwks without JWT_AUDIENCE: any token your identity provider issues "
                "for ANY application will be accepted. Set JWT_AUDIENCE (and JWT_ISSUER)."
            )
    get_token_verifier()  # unknown backend / missing variables fail here, at boot


def _scopes(claims: dict):
    """
    Scopes from the claim named by JWT_SCOPES_CLAIM (default "scopes"), falling back to the
    OAuth2-standard space-delimited "scope" claim when that one is absent, so tokens from most
    identity providers work without a custom claims mapper. The default claim must be a JSON
    list; a custom claim may also be a space-delimited string. Anything malformed is returned
    as-is and rejected by TokenPayload validation.
    """
    configured = os.environ.get("JWT_SCOPES_CLAIM", "scopes")
    if configured in claims:
        value = claims[configured]
        return value.split() if isinstance(value, str) and configured != "scopes" else value
    scope = claims.get("scope")
    return scope.split() if isinstance(scope, str) else []


_BEARER_HEADERS = {"WWW-Authenticate": "Bearer"}


def _unauthorized(error_code: str, message: str) -> AppError:
    return AppError(401, error_code, message, headers=_BEARER_HEADERS)


def verify_token(token: str) -> TokenPayload:
    """Verify a bearer token with the active TokenVerifier. Raises HTTP 401 on any failure."""
    try:
        claims = get_token_verifier().verify(token)
        return TokenPayload.model_validate({**claims, "scopes": _scopes(claims)})
    except TokenExpiredError:
        raise _unauthorized("TOKEN_EXPIRED", "Token has expired")
    except (InvalidTokenError, ValidationError):
        raise _unauthorized("INVALID_TOKEN", "Invalid token")


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> TokenPayload:
    """FastAPI dependency — extracts and validates the Bearer JWT."""
    if credentials is None:
        raise _unauthorized("MISSING_TOKEN", "Authorization header missing or malformed")
    return verify_token(credentials.credentials)


def require_scope(*scopes: str):
    """
    FastAPI dependency factory — enforces JWT scope claims on a route.

    Usage:
        @router.post("/items", ...)
        async def create_item(
            current_user: TokenPayload = Depends(require_scope("items:write")),
        ): ...

    The JWT must include a "scopes" claim (list of strings) containing all
    required scopes. Returns 403 if any scope is missing, 401 if token invalid.

    Scopes are additive — Depends(require_scope("items:read", "items:write"))
    requires both scopes to be present.
    """
    def _check(current_user: TokenPayload = Depends(get_current_user)) -> TokenPayload:
        token_scopes = set(current_user.scopes)
        missing = set(scopes) - token_scopes
        if missing:
            raise AppError(
                403,
                "INSUFFICIENT_SCOPE",
                f"Missing required scopes: {', '.join(sorted(missing))}",
            )
        return current_user
    return _check


# ── WebSocket tickets ──────────────────────────────────────────────────────
# Browsers cannot set an Authorization header on `new WebSocket(...)`, so a
# client first trades its Bearer JWT for a short-lived, single-purpose ticket
# and passes that in the query string. The ticket is a signed JWT with its own
# audience, so it is useless as an API token (and API tokens are useless as
# tickets). Verification is stateless — any container can do it. Single use is
# enforced by the TicketReplayStore (src/core/ports/ticket_store.py): shared
# across containers with EVENT_BUS_BACKEND=postgres, per container otherwise.

WS_TICKET_AUDIENCE = "ws-ticket"


def create_ws_ticket(user: TokenPayload) -> tuple[str, int]:
    """Mint a ticket for `user`. Returns (ticket, ttl_seconds)."""
    from src.api.event_access import TICKET_SCOPES  # local: event_access imports this module's models

    now = int(time.time())
    claims: dict = {
        "sub": user.sub,
        "aud": WS_TICKET_AUDIENCE,
        # Least privilege: only the scopes stream access can need, nothing that writes.
        "scopes": sorted(s for s in user.scopes if s in TICKET_SCOPES),
        "iat": now,
        "exp": now + WS_TICKET_TTL_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    issuer = os.environ.get("JWT_ISSUER")
    if issuer:
        claims["iss"] = issuer
    ticket = jwt.encode(claims, _ticket_secret(), algorithm=_ticket_algorithm())
    return ticket, WS_TICKET_TTL_SECONDS


def verify_ws_ticket(ticket: str) -> tuple[TokenPayload, str]:
    """
    Validate a ticket's signature, audience and expiry. Returns (claims, jti).
    Raises AppError(401) on any failure. Does NOT consume the ticket — the caller
    must consume the jti from the TicketReplayStore.
    """
    kwargs: dict = {"audience": WS_TICKET_AUDIENCE}
    issuer = os.environ.get("JWT_ISSUER")
    if issuer:
        kwargs["issuer"] = issuer
    try:
        claims = jwt.decode(
            ticket,
            _ticket_secret(),
            algorithms=[_ticket_algorithm()],
            options={"require": ["exp", "sub", "jti"]},
            **kwargs,
        )
        return TokenPayload.model_validate(claims), str(claims["jti"])
    except jwt.ExpiredSignatureError:
        raise _unauthorized("TICKET_EXPIRED", "Ticket has expired")
    except (jwt.InvalidTokenError, ValidationError):
        raise _unauthorized("INVALID_TICKET", "Invalid ticket")
