import os
import time
import pytest_asyncio

# Must be set before any src imports so auth._get_secret_key() resolves.
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-at-least-32-bytes")

import jwt
import pytest
from fastapi.testclient import TestClient

from pathlib import Path

from src.utils.dburl import describe_url, is_local, mask_url, url_problems
from src.utils.envfile import load_env_file

# ── Local Postgres/Supabase settings for the test suite ─────────────────────
# .env (git-ignored) may hold TEST_* settings. ONLY test settings are read here:
# loading the app's own DATABASE_URL / PERSISTENCE_BACKEND would make every
# TestClient start connect to a real database. A real `export` always wins.
ENV_FILE_PREFIXES = ("TEST_", "ALLOW_DESTRUCTIVE_DB_TESTS", "DB_POOL_")
load_env_file(Path(__file__).resolve().parent.parent / ".env", include_prefixes=ENV_FILE_PREFIXES)


def _guard_test_database() -> None:
    """The Postgres tests DROP the app's tables: refuse to point them at a remote DB by accident."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return
    for name, role in (("TEST_DATABASE_URL", "pool"), ("TEST_LISTEN_DATABASE_URL", "listen")):
        value = os.environ.get(name)
        if not value:
            continue
        errors, _ = url_problems(value, role=role)
        if errors:
            pytest.exit(f"{name} ({mask_url(value)}) is not usable:\n  - " + "\n  - ".join(errors), returncode=2)
    if not is_local(url) and os.environ.get("ALLOW_DESTRUCTIVE_DB_TESTS") != "yes":
        pytest.exit(
            f"Refusing to run: TEST_DATABASE_URL ({mask_url(url)}) is not localhost, and the Postgres tests DROP "
            "AND RECREATE the items, events, webhook_events and ws_ticket_uses tables.\n"
            "Use a dedicated, empty Supabase project, then confirm with ALLOW_DESTRUCTIVE_DB_TESTS=yes "
            "(in .env or the environment). Run `python scripts/check_supabase.py` first.",
            returncode=2,
        )


_guard_test_database()

from src.main import app
from src.api.auth import get_current_user
from src.api.models import TokenPayload
from src.deps import get_event_bus, get_item_service, get_ticket_store, get_webhook_store
from src.adapters.events.memory import InMemoryEventBus, InMemoryTicketStore, InMemoryWebhookStore
from src.adapters.persistence.memory import InMemoryItemRepository
from src.api import ratelimit, streaming
from src.core.services.item_service import ItemService

TEST_SECRET = "test-secret-key-for-pytest-at-least-32-bytes"
TEST_USER = TokenPayload(
    sub="test@example.com",
    exp=9_999_999_999,
    scopes=["items:read", "items:write"],
)


def make_token(
    sub: str = "test@example.com",
    exp_offset: int = 3600,
    secret: str = TEST_SECRET,
    algorithm: str = "HS256",
    scopes: list[str] | None = None,
    include_exp: bool = True,
    include_sub: bool = True,
    extra_claims: dict | None = None,
) -> str:
    now = int(time.time())
    payload: dict = {"iat": now}
    if include_sub:
        payload["sub"] = sub
    if include_exp:
        payload["exp"] = now + exp_offset
    if scopes is not None:
        payload["scopes"] = scopes
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, secret, algorithm=algorithm)


@pytest.fixture(autouse=True)
def event_bus():
    """A fresh in-memory event bus per test, shared by the item service and streams."""
    bus = InMemoryEventBus()
    app.dependency_overrides[get_event_bus] = lambda: bus
    yield bus
    app.dependency_overrides.pop(get_event_bus, None)


@pytest.fixture(autouse=True)
def webhook_store():
    store = InMemoryWebhookStore()
    app.dependency_overrides[get_webhook_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_webhook_store, None)


@pytest.fixture(autouse=True)
def ticket_store():
    store = InMemoryTicketStore()
    app.dependency_overrides[get_ticket_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_ticket_store, None)


@pytest.fixture(autouse=True)
def reset_stream_state():
    """Fresh connection cap and rate-limit buckets for every test."""
    original = (streaming.limiter, ratelimit.ticket_limiter, ratelimit.webhook_limiter)
    streaming.limiter = streaming.StreamLimiter(50)
    ratelimit.ticket_limiter = ratelimit._per_minute(10_000, burst=10_000)
    ratelimit.webhook_limiter = ratelimit._per_minute(100_000, burst=100_000)
    yield streaming.limiter
    streaming.limiter, ratelimit.ticket_limiter, ratelimit.webhook_limiter = original


@pytest.fixture(autouse=True)
def fresh_item_service(event_bus):
    """
    Each test gets an isolated in-memory repository — no state bleed between tests.
    Overrides the get_item_service FastAPI dependency for the duration of each test.
    """
    service = ItemService(InMemoryItemRepository(), event_bus)
    app.dependency_overrides[get_item_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_item_service, None)


@pytest.fixture
def client(fresh_item_service):
    """TestClient with auth dependency overridden — use for item/business logic tests."""
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def auth_client(fresh_item_service):
    """TestClient with real JWT auth — use for auth-specific tests."""
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def valid_token() -> str:
    """Valid token with no scopes — passes auth, fails scope checks."""
    return make_token()


@pytest.fixture
def read_headers(read_token: str) -> dict:
    return {"Authorization": f"Bearer {read_token}"}


@pytest.fixture
def read_token() -> str:
    return make_token(scopes=["items:read"])


@pytest.fixture
def write_token() -> str:
    return make_token(scopes=["items:read", "items:write"])


@pytest.fixture
def expired_token() -> str:
    return make_token(exp_offset=-3600)


@pytest.fixture
def wrong_secret_token() -> str:
    return make_token(secret="completely-wrong-secret-at-least-32-bytes")


@pytest.fixture
def auth_headers(valid_token: str) -> dict:
    return {"Authorization": f"Bearer {valid_token}"}


@pytest.fixture
def write_headers(write_token: str) -> dict:
    return {"Authorization": f"Bearer {write_token}"}


# ── Postgres (optional) ─────────────────────────────────────────────────────
# Set TEST_DATABASE_URL to a DISPOSABLE database to run the Postgres-backed
# contract tests; tables are dropped and recreated for every test.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def _listen_url():
    """A SESSION-level URL for LISTEN tests: never the transaction pooler (port 6543)."""
    explicit = os.environ.get("TEST_LISTEN_DATABASE_URL")
    if explicit:
        return explicit
    if TEST_DATABASE_URL and describe_url(TEST_DATABASE_URL).kind != "transaction-pooler":
        return TEST_DATABASE_URL
    return None


LISTEN_URL = _listen_url()
requires_listen = pytest.mark.skipif(
    not LISTEN_URL,
    reason="needs a session-level connection: set TEST_LISTEN_DATABASE_URL "
           "(Supabase: the session pooler or direct string, port 5432)",
)
BACKENDS = [
    "memory",
    pytest.param("postgres", marks=requires_postgres),
]


async def wipe_database(pool):
    """Drop everything the app (and its migrations) create, including migration bookkeeping."""
    await pool.execute(
        "DROP TABLE IF EXISTS items, events, webhook_events, ws_ticket_uses, rate_limits, schema_migrations CASCADE; "
        "DROP FUNCTION IF EXISTS publish_event(text, text, jsonb)"
    )


async def fresh_pg_pool():
    """A pool on a clean, fully migrated schema. The caller closes it."""
    from src.adapters.persistence.postgres import create_pool, ensure_schema

    pool = await create_pool(TEST_DATABASE_URL)
    await wipe_database(pool)
    await ensure_schema(pool)
    return pool


@pytest_asyncio.fixture
async def pg_pool():
    pool = await fresh_pg_pool()
    yield pool
    await pool.close()
