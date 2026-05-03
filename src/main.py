import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.models import ErrorDetail
from src.api.routes import router

from modal_common import get_env_config

env_config = get_env_config(os.environ.get("MODAL_ENV", "dev"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_NAME = f'{env_config.app_name}-{env_config.env_name}'

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {APP_NAME}")
    yield
    logger.info(f"Shutting down {APP_NAME}")


app = FastAPI(
    title=APP_NAME,
    description=env_config.app_description,
    version=env_config.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


app.include_router(router, prefix=env_config.server_prefix)
