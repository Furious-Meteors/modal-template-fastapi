import os
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pydantic import ValidationError

from src.api.models import ErrorDetail, TokenPayload

_bearer = HTTPBearer(auto_error=False)

MIN_SECRET_LENGTH = 32


def _get_secret_key() -> str:
    key = os.environ.get("JWT_SECRET")
    if not key:
        raise RuntimeError("JWT_SECRET environment variable is not set")
    return key


def _get_algorithm() -> str:
    return os.environ.get("JWT_ALGORITHM", "HS256")


def validate_auth_config() -> None:
    """
    Fail-fast check for the JWT environment. Called from the app lifespan so a
    missing or weak secret stops the container at startup instead of surfacing
    as a 500 on the first authenticated request.
    """
    key = _get_secret_key()  # raises RuntimeError when missing/empty
    if len(key) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters long "
            f"(got {len(key)})"
        )


def _decode_options() -> dict:
    """Extra kwargs for jwt.decode: audience/issuer are enforced only when configured."""
    kwargs: dict = {}
    audience = os.environ.get("JWT_AUDIENCE")
    issuer = os.environ.get("JWT_ISSUER")
    if audience:
        kwargs["audience"] = audience
    if issuer:
        kwargs["issuer"] = issuer
    return kwargs


def _invalid_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=ErrorDetail(
            detail="Invalid token",
            session_id=str(uuid.uuid4()),
            error_code="INVALID_TOKEN",
        ).model_dump(),
        headers={"WWW-Authenticate": "Bearer"},
    )


def verify_token(token: str) -> TokenPayload:
    """Decode and validate a JWT into a TokenPayload. Raises HTTP 401 on any failure."""
    try:
        claims = jwt.decode(
            token,
            _get_secret_key(),
            algorithms=[_get_algorithm()],
            options={"require": ["exp", "sub"]},
            **_decode_options(),
        )
        return TokenPayload.model_validate(claims)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ErrorDetail(
                detail="Token has expired",
                session_id=str(uuid.uuid4()),
                error_code="TOKEN_EXPIRED",
            ).model_dump(),
            headers={"WWW-Authenticate": "Bearer"},
        )
    except (jwt.InvalidTokenError, ValidationError):
        raise _invalid_token()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> TokenPayload:
    """FastAPI dependency — extracts and validates the Bearer JWT."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ErrorDetail(
                detail="Authorization header missing or malformed",
                session_id=str(uuid.uuid4()),
                error_code="MISSING_TOKEN",
            ).model_dump(),
            headers={"WWW-Authenticate": "Bearer"},
        )
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
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ErrorDetail(
                    detail=f"Missing required scopes: {', '.join(sorted(missing))}",
                    session_id=str(uuid.uuid4()),
                    error_code="INSUFFICIENT_SCOPE",
                ).model_dump(),
            )
        return current_user
    return _check
