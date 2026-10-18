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

import logging
import os
from functools import lru_cache

from src.core.services.item_service import ItemService
from src.adapters.telemetry.tracing_proxy import TracingProxy

logger = logging.getLogger(__name__)

# ── Required env vars per backend ──────────────────────────────────────────
# Checked at startup so misconfiguration fails fast with a clear error,
# not silently at the first request that hits the DB.
# Update this dict when adding a new adapter.
_REQUIRED_ENV: dict[str, list[str]] = {
    "memory":   [],
    # "postgres": ["DATABASE_URL"],
    # "dynamo":   ["DYNAMO_TABLE", "AWS_REGION"],
}


# Event bus backends — same fail-fast rules as persistence.
_REQUIRED_EVENT_BUS_ENV: dict[str, list[str]] = {
    "memory": [],
    # "modal_queue": [],                # Modal Queue/Dict — shared across containers
    # "redis":       ["REDIS_URL"],
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
    return TracingProxy(raw, prefix="repository.item")


@lru_cache(maxsize=1)
def _get_event_bus():
    """
    Process-wide event bus used by the item service and the streaming endpoints.
    Same seam as the repository: add a branch per backend below.
    """
    backend = os.environ.get("EVENT_BUS_BACKEND", "memory")
    if backend not in _REQUIRED_EVENT_BUS_ENV:
        raise ValueError(
            f"Unknown EVENT_BUS_BACKEND='{backend}'. "
            f"Known backends: {', '.join(_REQUIRED_EVENT_BUS_ENV)}. "
            f"Add an adapter in src/adapters/events/ and register it here."
        )
    missing = [v for v in _REQUIRED_EVENT_BUS_ENV[backend] if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(
            f"EVENT_BUS_BACKEND='{backend}' requires env vars that are not set: "
            f"{', '.join(missing)}"
        )

    if backend == "memory":
        from src.adapters.events.memory import InMemoryEventBus
        raw = InMemoryEventBus()

    # ── Add new backends here ───────────────────────────────────────────────
    # elif backend == "modal_queue":
    #     from src.adapters.events.modal_queue import ModalQueueEventBus
    #     raw = ModalQueueEventBus()   # must satisfy src/core/ports/event_bus.py
    # ────────────────────────────────────────────────────────────────────────
    else:
        raise ValueError(f"Backend '{backend}' is registered but has no factory. Add one above.")

    return TracingProxy(raw, prefix="event_bus")


@lru_cache(maxsize=1)
def _get_webhook_store():
    # Shares EVENT_BUS_BACKEND's lifecycle: the in-memory store is per-process too.
    from src.adapters.events.memory import InMemoryWebhookStore
    return InMemoryWebhookStore()


def init_dependencies() -> None:
    """
    Eagerly build and validate everything the request path needs.

    Called from the app lifespan so a bad PERSISTENCE_BACKEND or missing backend
    env var stops the container at startup instead of 500-ing the first request.
    """
    backend = os.environ.get("PERSISTENCE_BACKEND", "memory")
    _get_repository()
    _get_event_bus()
    _get_webhook_store()
    if os.environ.get("EVENT_BUS_BACKEND", "memory") == "memory" and os.environ.get("MODAL_ENV") == "prod":
        logger.warning(
            "EVENT_BUS_BACKEND=memory in prod: SSE/WebSocket streams only see events "
            "published on their own container, and webhook de-duplication is per "
            "container. Use a shared event-bus adapter before scaling out."
        )
    if backend == "memory" and os.environ.get("MODAL_ENV") == "prod":
        logger.warning(
            "PERSISTENCE_BACKEND=memory in prod: each container keeps its own "
            "store, so data differs between containers and is lost on scale-down. "
            "Use a real persistence adapter."
        )


def get_event_bus():
    """FastAPI dependency — the process-wide EventBus."""
    return _get_event_bus()


def get_webhook_store():
    """FastAPI dependency — the webhook idempotency store."""
    return _get_webhook_store()


def get_item_service() -> ItemService:
    """FastAPI dependency — injected into routes via Depends(get_item_service)."""
    return ItemService(_get_repository(), _get_event_bus())
