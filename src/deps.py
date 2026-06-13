"""
src/deps.py
===========
Dependency wiring — the only file that knows which concrete adapter is active.

This is the single seam between the application core and infrastructure.
Swapping the persistence backend requires:
  1. Adding a new adapter under src/adapters/persistence/
  2. Adding one branch here keyed on PERSISTENCE_BACKEND
  3. Setting PERSISTENCE_BACKEND in your Modal secret or local .env

No routes, services, or domain files need to change.

Caching layer (optional):
  Wrap the repository with a cache adapter before passing it to ItemService.
  Example:
    repo = PostgresItemRepository(os.environ["DATABASE_URL"])
    cache = RedisCache(os.environ["REDIS_URL"])
    return ItemService(CachedItemRepository(repo, cache))
"""
from __future__ import annotations

import os
from functools import lru_cache

from src.core.services.item_service import ItemService
from src.adapters.telemetry.tracing_proxy import TracingProxy

# ── Required env vars per backend ──────────────────────────────────────────
# Checked at startup so misconfiguration fails fast with a clear error,
# not silently at the first request that hits the DB.
# Update this dict when adding a new adapter.
_REQUIRED_ENV: dict[str, list[str]] = {
    "memory":   [],
    # "postgres": ["DATABASE_URL"],
    # "dynamo":   ["DYNAMO_TABLE", "AWS_REGION"],
}


def _validate_backend(backend: str) -> None:
    if backend not in _REQUIRED_ENV:
        raise ValueError(
            f"Unknown PERSISTENCE_BACKEND='{backend}'. "
            f"Known backends: {', '.join(_REQUIRED_ENV)}. "
            f"Add an adapter in src/adapters/persistence/ and register it here."
        )
    missing = [v for v in _REQUIRED_ENV[backend] if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(
            f"PERSISTENCE_BACKEND='{backend}' requires env vars that are not set: "
            f"{', '.join(missing)}"
        )


@lru_cache(maxsize=1)
def _get_repository():
    """
    Instantiated once per process lifetime. For adapters that manage
    connection pools (asyncpg, motor, etc.), open the pool in main.py's
    lifespan instead, and pass it here via a module-level variable.
    """
    backend = os.environ.get("PERSISTENCE_BACKEND", "memory")
    _validate_backend(backend)

    if backend == "memory":
        from src.adapters.persistence.memory import InMemoryItemRepository
        raw = InMemoryItemRepository()

    # ── Add new backends here ───────────────────────────────────────────────
    # elif backend == "postgres":
    #     from src.adapters.persistence.postgres import PostgresItemRepository
    #     raw = PostgresItemRepository(os.environ["DATABASE_URL"])
    #
    # elif backend == "dynamo":
    #     from src.adapters.persistence.dynamo import DynamoItemRepository
    #     raw = DynamoItemRepository(os.environ["DYNAMO_TABLE"])
    # ────────────────────────────────────────────────────────────────────────
    else:
        raise ValueError(f"Backend '{backend}' is in _REQUIRED_ENV but has no factory. Add one above.")

    # Wrap with telemetry sidecar — spans become children of the HTTP middleware span.
    # Remove this line to disable sub-request tracing with zero other changes.
    return TracingProxy(raw, prefix=f"repository.item")


def get_item_service() -> ItemService:
    """FastAPI dependency — injected into routes via Depends(get_item_service)."""
    return ItemService(_get_repository())
