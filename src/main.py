import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.auth import validate_auth_config
from src.api.errors import register_exception_handlers
from src.api.events import router as events_router
from src.api.routes import router
from src.api.webhooks import router as webhooks_router
from src.adapters.http.middleware import TelemetryMiddleware
from src.deps import init_dependencies
from src.utils.config import APP_DESCRIPTION, APP_ENV, APP_VERSION, CORS_ORIGINS, SERVER_PREFIX, SERVICE_TITLE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast: a misconfigured container should not start serving.
    validate_auth_config()
    init_dependencies()
    if "*" in CORS_ORIGINS and APP_ENV == "prod":
        logger.warning("CORS_ORIGINS='*' in prod: set EnvConfig.cors_origins to your real origins.")
    logger.info(f"Starting {SERVICE_TITLE}")
    yield
    logger.info(f"Shutting down {SERVICE_TITLE}")


app = FastAPI(
    title=SERVICE_TITLE,
    description=APP_DESCRIPTION,
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,   # set per-env via EnvConfig.cors_origins → CORS_ORIGINS env var
    # Auth is a Bearer header, not cookies, so credentialed CORS is never needed;
    # it is also invalid alongside a wildcard origin.
    allow_credentials="*" not in CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Registered last → executes outermost, so it wraps CORS, auth, validation errors, and 404s.
app.add_middleware(TelemetryMiddleware)


register_exception_handlers(app)

app.include_router(router, prefix=SERVER_PREFIX)
app.include_router(events_router, prefix=SERVER_PREFIX)
app.include_router(webhooks_router, prefix=SERVER_PREFIX)
