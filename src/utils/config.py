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
