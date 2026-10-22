"""
Run in a subprocess by tests/test_business_metrics_integration.py: drives the REAL
app wiring (deps -> sidecars -> global MeterProvider) and prints what was recorded.
"""
import asyncio
import json
import os

os.environ.setdefault("JWT_SECRET", "x" * 40)

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

from fastapi.testclient import TestClient  # noqa: E402

from src.api.auth import get_current_user  # noqa: E402
from src.api.models import TokenPayload  # noqa: E402
from src.main import app  # noqa: E402

app.dependency_overrides[get_current_user] = lambda: TokenPayload(
    sub="u", exp=9_999_999_999, scopes=["items:read", "items:write"]
)

with TestClient(app) as client:  # real deps: real service, real bus, no test overrides
    created = client.post("/api/v1/items", json={"name": "probe"}).json()["data"]["id"]
    client.post("/api/v1/items", json={"name": "PROBE "})            # duplicate -> 409
    client.get(f"/api/v1/items/{created}")
    client.get("/api/v1/items/ghost")                                 # 404
    client.put(f"/api/v1/items/{created}", json={"name": "probe 2"})
    client.delete(f"/api/v1/items/{created}")

out = []
for rm in reader.get_metrics_data().resource_metrics:
    for sm in rm.scope_metrics:
        for m in sm.metrics:
            if m.name.startswith("app."):
                for p in m.data.data_points:
                    out.append({"name": m.name, "unit": m.unit, "attrs": dict(p.attributes),
                                "value": getattr(p, "value", None), "count": getattr(p, "count", None)})
print(json.dumps(out))
