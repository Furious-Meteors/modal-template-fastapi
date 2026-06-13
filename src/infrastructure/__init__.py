"""
src/infrastructure
==================
Cross-cutting infrastructure layer — telemetry, observability, SDK wiring.

Sits outside the hexagon core. Neither domain nor adapters import from here
directly; telemetry is injected as a sidecar via TracingProxy in deps.py.

Template-owned files (copy verbatim, never edit per-service):
    setup.py       — OTel SDK init, OTLP/Grafana Cloud exporter, providers
    __init__.py    — this file

Service-owned file (replace entirely per service):
    metrics.py     — domain-specific metric instruments

Public surface:
    setup_telemetry()   call once in Modal @enter hook
    get_tracer(name?)   get a named tracer (used by TracingProxy + adapters)
    get_meter(name?)    get a named meter (used in metrics.py)

Note: TelemetryMiddleware lives in src/adapters/http/middleware.py — it is an
inbound HTTP adapter, not infrastructure setup.
"""

from src.infrastructure.setup import get_meter, get_tracer, record_cold_start, setup_telemetry

__all__ = [
    "setup_telemetry",
    "record_cold_start",
    "get_tracer",
    "get_meter",
]