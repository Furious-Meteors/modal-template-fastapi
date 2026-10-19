"""
src/utils/config.py
===========================
Application-level config read exclusively from environment variables.

All values here are set by modal_common.configure_env_vars() before src/ is
ever imported in a Modal container. For local uvicorn runs or tests, sensible
defaults are provided so no Modal dependency is needed.

This file must never import from modal_common or modal.
"""
import os

APP_NAME        = os.environ.get("APP_NAME", "modal-template-fastapi")
APP_ENV         = os.environ.get("MODAL_ENV", "dev")
APP_VERSION     = os.environ.get("APP_VERSION", "1.0.0")
APP_DESCRIPTION = os.environ.get("APP_DESCRIPTION", "A FastAPI template deployed on Modal with CRUD endpoints.")
SERVER_PREFIX   = os.environ.get("SERVER_PREFIX", "/api/v1")
CORS_ORIGINS    = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

SERVICE_TITLE = f"{APP_NAME}-{APP_ENV}"

# FastAPI >= 0.143 emits its own request spans and metrics. TelemetryMiddleware
# already records those (plus session IDs, route templates and WebSocket close
# codes), so the native ones would double every server span and put a second,
# differently-unit "http.server.request.duration" next to ours. Off by default;
# set FASTAPI_NATIVE_TELEMETRY=true to restore FastAPI's own (it then also adds
# fastapi.dependencies/endpoint/serialization child spans — but you get two
# server spans per request, so consider disabling TelemetryMiddleware's export).
FASTAPI_NATIVE_TELEMETRY = os.environ.get("FASTAPI_NATIVE_TELEMETRY", "").strip().lower() == "true"


# ── Streaming (SSE / WebSocket) and webhooks ────────────────────────────────
def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer (got {raw!r})") from None
    if value < 1:
        raise RuntimeError(f"{name} must be >= 1 (got {value})")
    return value


# Concurrent SSE + WebSocket connections per container. modal_common keeps this
# below max_concurrent_requests so streams can never take every request slot.
STREAM_MAX_CONNECTIONS = _positive_int("STREAM_MAX_CONNECTIONS", 50)
STREAM_HEARTBEAT_SECONDS = _positive_int("STREAM_HEARTBEAT_SECONDS", 15)
# Streams end themselves after this long (clients reconnect with Last-Event-ID),
# comfortably inside the Modal request timeout.
STREAM_MAX_DURATION_SECONDS = _positive_int("STREAM_MAX_DURATION_SECONDS", 120)
WS_MAX_MESSAGE_BYTES = _positive_int("WS_MAX_MESSAGE_BYTES", 4096)
WS_TICKET_TTL_SECONDS = _positive_int("WS_TICKET_TTL_SECONDS", 30)
WEBHOOK_TOLERANCE_SECONDS = _positive_int("WEBHOOK_TOLERANCE_SECONDS", 300)
WEBHOOK_MAX_BODY_BYTES = _positive_int("WEBHOOK_MAX_BODY_BYTES", 1_048_576)

# ── Shared streaming state (EVENT_BUS_BACKEND=postgres) ─────────────────────
EVENT_BUS_POLL_MS = _positive_int("EVENT_BUS_POLL_MS", 1000)        # fallback poll / no-LISTEN latency
EVENT_RETENTION_SECONDS = _positive_int("EVENT_RETENTION_SECONDS", 3600)
EVENT_REPLAY_SIZE = _positive_int("EVENT_REPLAY_SIZE", 100)
WEBHOOK_IDEMPOTENCY_TTL_SECONDS = _positive_int("WEBHOOK_IDEMPOTENCY_TTL_SECONDS", 86_400)

# ── Rate limits (per container, in-memory token buckets) ────────────────────
# Keys are chosen so a shared proxy IP can never lock out legitimate callers:
# the ticket limit is per authenticated user, the webhook limit per provider
# (generous — providers retry on 429), the message limit per connection.
RATE_TICKET_PER_MINUTE = _positive_int("RATE_TICKET_PER_MINUTE", 30)
RATE_WEBHOOK_PER_MINUTE = _positive_int("RATE_WEBHOOK_PER_MINUTE", 1200)
RATE_WS_MESSAGES_PER_SECOND = _positive_int("RATE_WS_MESSAGES_PER_SECOND", 10)
