import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.models import ErrorDetail
from src.api.routes import router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting modal-template-fastapi")
    yield
    logger.info("Shutting down modal-template-fastapi")


app = FastAPI(
    title="modal-template-fastapi",
    description="A FastAPI template deployed on Modal with CRUD endpoints.",
    version="1.0.0",
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


app.include_router(router, prefix="/api/v1")
