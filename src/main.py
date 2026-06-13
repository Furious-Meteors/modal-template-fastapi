import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.models import ErrorDetail
from src.api.routes import router
from src.adapters.http.middleware import TelemetryMiddleware
from src.utils.config import APP_DESCRIPTION, APP_VERSION, CORS_ORIGINS, SERVER_PREFIX, SERVICE_TITLE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Registered last → executes outermost, so it wraps CORS, auth, validation errors, and 404s.
app.add_middleware(TelemetryMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=ErrorDetail(
            detail=str(exc),
            session_id=str(uuid.uuid4()),
            error_code="VALIDATION_ERROR",
        ).model_dump(),
    )


app.include_router(router, prefix=SERVER_PREFIX)
