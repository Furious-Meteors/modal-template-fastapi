"""
Run in a subprocess by tests/test_telemetry.py: telemetry providers are
process-global, so the check needs a clean interpreter. Prints one JSON object.
"""
import json
import os

os.environ.setdefault("JWT_SECRET", "x" * 40)

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402

with TestClient(app) as client:
    client.get("/api/v1/health")
    client.get("/definitely/not/a/route")

spans = [
    {"name": s.name, "kind": s.kind.name}
    for s in exporter.get_finished_spans()
]
instruments = sorted(
    {(m.name, m.unit) for rm in reader.get_metrics_data().resource_metrics
     for sm in rm.scope_metrics for m in sm.metrics}
)
print(json.dumps({"spans": spans, "metrics": instruments}))
