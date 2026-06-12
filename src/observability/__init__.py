"""
src/observability
=================
OpenTelemetry instrumentation layer for modal-template-fastapi.

Template-owned files (copy verbatim, never edit per-service):
    setup.py       — SDK init, OTLP/Grafana Cloud exporter
    middleware.py  — FastAPI auto-instrumentation
    __init__.py    — this file

Service-owned file (replace entirely per service):
    metrics.py     — domain-specific metric instruments

Public surface:
    setup_telemetry()      call once in Modal @enter hook
    get_tracer(name?)      get a named tracer anywhere
    get_meter(name?)       get a named meter (used in metrics.py)
    TelemetryMiddleware    add to FastAPI app in main.py
"""

from src.observability.setup import get_meter, get_tracer, setup_telemetry
from src.observability.middleware import TelemetryMiddleware

__all__ = [
    "setup_telemetry",
    "get_tracer",
    "get_meter",
    "TelemetryMiddleware",
]