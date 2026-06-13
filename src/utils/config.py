"""
src/config.py
=============
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

SERVICE_TITLE = f"{APP_NAME}-{APP_ENV}"
