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
from src.adapters.telemetry.metered import MeteredEventBus, MeteredItemService
from src.adapters.telemetry.tracing_proxy import TracingProxy

logger = logging.getLogger(__name__)

# ── Required env vars per backend ──────────────────────────────────────────
# Checked at startup so misconfiguration fails fast with a clear error,
# not silently at the first request that hits the DB.
# Update this dict when adding a new adapter.
_REQUIRED_ENV: dict[str, list[str]] = {
    "memory":   [],
    "postgres": ["DATABASE_URL"],   # Supabase pooler URL — see adapters/persistence/postgres.py
    # "dynamo":   ["DYNAMO_TABLE", "AWS_REGION"],
}


# Event bus backends — same fail-fast rules as persistence.
# EVENT_BUS_BACKEND selects the streaming state as a unit: the event bus, the
# webhook idempotency store and the WebSocket ticket replay store all follow it,
# so they are either all shared across containers or all per-container.
_REQUIRED_EVENT_BUS_ENV: dict[str, list[str]] = {
    "memory":   [],
    "postgres": ["DATABASE_URL"],   # shared across containers; see docs/persistence.md
    # "redis":  ["REDIS_URL"],
}


def _event_bus_backend() -> str:
    return os.environ.get("EVENT_BUS_BACKEND", "memory")


def _needs_pool(persistence: str, event_bus: str) -> bool:
    return persistence == "postgres" or event_bus == "postgres"


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


# Connection pool for backends that need one. Opened in the app lifespan
# (init_resources) — never at import time — so it is created after a Modal
# snapshot restore, when sockets are valid, and closed on shutdown.
_pool = None


async def init_resources() -> None:
    """Open long-lived connections for the configured persistence backend."""
    global _pool
    backend = os.environ.get("PERSISTENCE_BACKEND", "memory")
    _validate_backend(backend)
    bus_backend = _event_bus_backend()
    _validate_event_bus_backend(bus_backend)
    if _needs_pool(backend, bus_backend) and _pool is None:
        from src.adapters.persistence.postgres import create_pool, ensure_schema

        _pool = await create_pool(os.environ["DATABASE_URL"])
        try:
            await ensure_schema(_pool)
        except Exception:
            await close_resources()
            raise
        for cached in (_get_repository, _get_event_bus, _get_webhook_store, _get_ticket_store):
            cached.cache_clear()  # rebuild against the live pool
        logger.info("Postgres pool ready (max_size=%s)", _pool.get_max_size())
    if bus_backend == "postgres":
        await _get_event_bus().start()  # begins the cross-container event pump


async def close_resources() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None and _event_bus_backend() == "postgres":
        try:
            await _get_event_bus().close()  # stop the pump before its pool goes away
        except Exception:
            logger.warning("Event bus did not close cleanly", exc_info=True)
    for cached in (_get_repository, _get_event_bus, _get_webhook_store, _get_ticket_store):
        cached.cache_clear()
    if pool is not None:
        await pool.close()


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

    elif backend == "postgres":
        if _pool is None:
            raise RuntimeError(
                "PERSISTENCE_BACKEND=postgres but the connection pool is not open; "
                "init_resources() must run first (the app lifespan does this)."
            )
        from src.adapters.persistence.postgres import PostgresItemRepository
        raw = PostgresItemRepository(_pool)

    # ── Add new backends here ───────────────────────────────────────────────
    # elif backend == "dynamo":
    #     from src.adapters.persistence.dynamo import DynamoItemRepository
    #     raw = DynamoItemRepository(os.environ["DYNAMO_TABLE"])
    # ────────────────────────────────────────────────────────────────────────
    else:
        raise ValueError(f"Backend '{backend}' is in _REQUIRED_ENV but has no factory. Add one above.")

    # Wrap with telemetry sidecar — spans become children of the HTTP middleware span.
    # Remove this line to disable sub-request tracing with zero other changes.
    return TracingProxy(raw, prefix="repository.item")


def _validate_event_bus_backend(backend: str) -> None:
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


def _require_pool(what: str):
    if _pool is None:
        raise RuntimeError(
            f"{what} needs the Postgres connection pool, which is not open; "
            "init_resources() must run first (the app lifespan does this)."
        )
    return _pool


@lru_cache(maxsize=1)
def _get_event_bus():
    """
    Process-wide event bus used by the item service and the streaming endpoints.
    Same seam as the repository: add a branch per backend below.
    """
    backend = _event_bus_backend()
    _validate_event_bus_backend(backend)

    if backend == "memory":
        from src.adapters.events.memory import InMemoryEventBus
        raw = InMemoryEventBus()
    elif backend == "postgres":
        from src.adapters.events.postgres import PostgresEventBus
        from src.utils.config import EVENT_BUS_POLL_MS, EVENT_REPLAY_SIZE, EVENT_RETENTION_SECONDS

        raw = PostgresEventBus(
            _require_pool("EVENT_BUS_BACKEND=postgres"),
            # LISTEN needs a session-level connection (direct URL or session pooler);
            # unset = poll through the regular pool. See docs/persistence.md.
            listen_dsn=os.environ.get("EVENT_BUS_LISTEN_URL") or None,
            poll_interval=EVENT_BUS_POLL_MS / 1000,
            replay_size=EVENT_REPLAY_SIZE,
            retention_seconds=EVENT_RETENTION_SECONDS,
        )

    # ── Add new backends here ───────────────────────────────────────────────
    # elif backend == "redis":
    #     from src.adapters.events.redis import RedisEventBus
    #     raw = RedisEventBus(os.environ["REDIS_URL"])   # must satisfy src/core/ports/event_bus.py
    # ────────────────────────────────────────────────────────────────────────
    else:
        raise ValueError(f"Backend '{backend}' is registered but has no factory. Add one above.")

    # Telemetry sidecars, outermost last: metrics wrap tracing wrap the adapter.
    return MeteredEventBus(TracingProxy(raw, prefix="event_bus"))


@lru_cache(maxsize=1)
def _get_webhook_store():
    if _event_bus_backend() == "postgres":
        from src.adapters.events.postgres import PostgresWebhookStore
        from src.utils.config import WEBHOOK_IDEMPOTENCY_TTL_SECONDS

        return TracingProxy(
            PostgresWebhookStore(_require_pool("The webhook store"), WEBHOOK_IDEMPOTENCY_TTL_SECONDS),
            prefix="webhook_store",
        )
    from src.adapters.events.memory import InMemoryWebhookStore
    return InMemoryWebhookStore()


@lru_cache(maxsize=1)
def _get_ticket_store():
    if _event_bus_backend() == "postgres":
        from src.adapters.events.postgres import PostgresTicketStore
        return TracingProxy(PostgresTicketStore(_require_pool("The ticket store")), prefix="ticket_store")
    from src.adapters.events.memory import InMemoryTicketStore
    return InMemoryTicketStore()


def _refuse_memory_in_prod(persistence: str, event_bus: str) -> None:
    """
    In-memory storage is per container and lost on scale-down. In prod that silently
    loses data, so refuse to start — loudly, at boot — unless someone explicitly accepts it.
    """
    if os.environ.get("MODAL_ENV") != "prod":
        return
    if os.environ.get("ALLOW_MEMORY_IN_PROD", "").strip().lower() == "true":
        return
    memory = [f"{name}=memory" for name, value in (("PERSISTENCE_BACKEND", persistence), ("EVENT_BUS_BACKEND", event_bus)) if value == "memory"]
    if memory:
        raise RuntimeError(
            f"Refusing to start in prod with in-memory storage ({', '.join(memory)}): data would be "
            "per container and lost on scale-down. Configure Postgres (PERSISTENCE_BACKEND=postgres, "
            "EVENT_BUS_BACKEND=postgres, DATABASE_URL — see docs/persistence.md), or set "
            "ALLOW_MEMORY_IN_PROD=true to accept the data loss."
        )


def init_dependencies() -> None:
    """
    Eagerly build and validate everything the request path needs.

    Called from the app lifespan so a bad PERSISTENCE_BACKEND or missing backend
    env var stops the container at startup instead of 500-ing the first request.
    """
    backend = os.environ.get("PERSISTENCE_BACKEND", "memory")
    _refuse_memory_in_prod(backend, _event_bus_backend())
    _get_repository()
    _get_event_bus()
    _get_webhook_store()
    _get_ticket_store()
    if _event_bus_backend() == "memory" and os.environ.get("MODAL_ENV") == "prod":
        logger.warning(
            "EVENT_BUS_BACKEND=memory in prod: SSE/WebSocket streams only see events "
            "published on their own container, and webhook de-duplication and "
            "WebSocket ticket replay protection are per container. Set "
            "EVENT_BUS_BACKEND=postgres before scaling out."
        )
    if _event_bus_backend() == "postgres" and not os.environ.get("EVENT_BUS_LISTEN_URL"):
        logger.info(
            "EVENT_BUS_LISTEN_URL is not set: events are picked up by polling "
            "(latency up to EVENT_BUS_POLL_MS). Set it to a session-level Postgres "
            "URL for instant delivery."
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


def get_ticket_store():
    """FastAPI dependency — the WebSocket ticket replay store."""
    return _get_ticket_store()


def get_item_service() -> MeteredItemService:
    """FastAPI dependency — injected into routes via Depends(get_item_service)."""
    return MeteredItemService(ItemService(_get_repository(), _get_event_bus()))
