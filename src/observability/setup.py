"""
src/observability/setup.py
==========================
OpenTelemetry SDK bootstrap. Call setup_telemetry() once inside
the Modal @enter hook — never at module import time.

This file is owned by the template. Do not edit it per-service.
Per-service configuration lives entirely in two env vars:
  OTEL_SERVICE_NAME            e.g. "modal-fastapi" / "ai-research-vault"
  OTEL_EXPORTER_OTLP_ENDPOINT  Grafana Cloud OTLP endpoint URL
  GRAFANA_OTLP_TOKEN           Grafana Cloud API token (via Modal Secret)
"""

from __future__ import annotations

import base64
import logging
import os
from functools import lru_cache

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

logger = logging.getLogger(__name__)

_INITIALISED = False


def _build_resource() -> Resource:
    return Resource.create({
        "service.name":            os.environ.get("OTEL_SERVICE_NAME", "modal-fastapi"),
        "service.version":         os.environ.get("OTEL_SERVICE_VERSION", "1.0.0"),
        "deployment.environment":  os.environ.get("MODAL_ENV", "dev"),
    })


def _grafana_headers() -> dict[str, str]:
    """
    Grafana Cloud OTLP uses HTTP Basic Auth:
      username  = numeric instance ID  (GRAFANA_INSTANCE_ID)
      password  = API token            (GRAFANA_OTLP_TOKEN)
    Both come in via a single Modal Secret.
    """
    instance_id = os.environ.get("GRAFANA_INSTANCE_ID", "")
    token       = os.environ.get("GRAFANA_OTLP_TOKEN", "")
    if not instance_id or not token:
        return {}
    creds = base64.b64encode(f"{instance_id}:{token}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def setup_telemetry() -> None:
    """
    Idempotent SDK bootstrap. Safe to call in @enter — runs once per
    container lifetime regardless of hot-reloads.

    Does nothing (logs a debug line) if OTEL_EXPORTER_OTLP_ENDPOINT
    is not set — so local dev without a Grafana account stays silent.
    """
    global _INITIALISED
    if _INITIALISED:
        return

    # ── Startup diagnostic — remove once telemetry is confirmed working ──
    _instance_id = os.environ.get("GRAFANA_INSTANCE_ID", "")
    _token       = os.environ.get("GRAFANA_OTLP_TOKEN", "")
    _grf_ep      = os.environ.get("GRAFANA_OTLP_ENDPOINT", "")
    _otlp_ep     = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    logger.info(
        "OTel secret check — "
        "GRAFANA_INSTANCE_ID=%s  "
        "GRAFANA_OTLP_TOKEN=%s  "
        "GRAFANA_OTLP_ENDPOINT=%s  "
        "OTEL_EXPORTER_OTLP_ENDPOINT=%s",
        _instance_id or "NOT SET",
        ("SET (starts: " + _token[:8] + "...)") if _token else "NOT SET",
        _grf_ep or "NOT SET",
        _otlp_ep or "NOT SET",
    )
    # ─────────────────────────────────────────────────────────────────────

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    resource  = _build_resource()
    headers   = _grafana_headers()

    if not endpoint:
        logger.debug(
            "OTEL_EXPORTER_OTLP_ENDPOINT not set — telemetry disabled. "
            "Set it in your Modal Secret to enable."
        )
        # Still set no-op providers so get_meter() / get_tracer() calls
        # in metrics.py don't raise AttributeErrors at runtime.
        trace.set_tracer_provider(TracerProvider(resource=resource))
        metrics.set_meter_provider(MeterProvider(resource=resource))
        _INITIALISED = True
        return

    # ── Traces ────────────────────────────────────────────────────────
    span_exporter = OTLPSpanExporter(
        endpoint=f"{endpoint}/v1/traces",
        headers=headers,
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            span_exporter,
            max_queue_size=2048,        # drop silently when full — never block
            max_export_batch_size=512,
            export_timeout_millis=5000,
        )
    )
    trace.set_tracer_provider(tracer_provider)

    # ── Metrics ───────────────────────────────────────────────────────
    metric_exporter = OTLPMetricExporter(
        endpoint=f"{endpoint}/v1/metrics",
        headers=headers,
    )
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=int(
            os.environ.get("OTEL_METRIC_EXPORT_INTERVAL_MS", "15000")
        ),
    )
    metrics.set_meter_provider(
        MeterProvider(resource=resource, metric_readers=[metric_reader])
    )

    # ── Log correlation ───────────────────────────────────────────────
    # Injects otelTraceID + otelSpanID into every Python LogRecord so that
    # any log line can be linked back to its trace in Grafana Loki/Tempo.
    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        LoggingInstrumentor().instrument()
    except ImportError:
        pass  # package optional; silently skip if not installed

    _INITIALISED = True
    logger.info(
        "OpenTelemetry initialised → %s  service=%s  env=%s",
        endpoint,
        os.environ.get("OTEL_SERVICE_NAME", "modal-fastapi"),
        os.environ.get("MODAL_ENV", "dev"),
    )


@lru_cache(maxsize=None)
def get_tracer(name: str | None = None) -> trace.Tracer:
    svc = os.environ.get("OTEL_SERVICE_NAME", "modal-fastapi")
    return trace.get_tracer(name or svc)


@lru_cache(maxsize=None)
def get_meter(name: str | None = None) -> metrics.Meter:
    svc = os.environ.get("OTEL_SERVICE_NAME", "modal-fastapi")
    return metrics.get_meter(name or svc)