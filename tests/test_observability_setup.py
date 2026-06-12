"""
Tests for src/observability/setup.py

All tests are fully isolated — they reset the global _INITIALISED flag and
clear the OTel global providers before and after each test so state never
leaks between tests.

No network calls are made. The OTLP exporters are never contacted because
either no endpoint is set (no-op path) or the exporter is patched out.
"""
import base64
import importlib
import os
import sys

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_otel_globals():
    """Reset OTel global providers and the module-level _INITIALISED flag."""
    import src.observability.setup as setup_mod
    setup_mod._INITIALISED = False
    # Clear lru_cache so get_tracer / get_meter return fresh objects
    setup_mod.get_tracer.cache_clear()
    setup_mod.get_meter.cache_clear()
    # Re-install no-op providers so subsequent calls don't reuse real ones
    trace.set_tracer_provider(TracerProvider())
    metrics.set_meter_provider(MeterProvider())


@pytest.fixture(autouse=True)
def isolated_otel(monkeypatch):
    """
    Clears OTel state before every test and restores original env vars after.
    autouse=True means every test in this file gets isolation for free.
    """
    _reset_otel_globals()
    yield
    _reset_otel_globals()


# ---------------------------------------------------------------------------
# setup_telemetry() — no-op path (no endpoint set)
# ---------------------------------------------------------------------------


def test_setup_telemetry_no_op_when_endpoint_missing(monkeypatch):
    """setup_telemetry() runs silently with no endpoint — no exception raised."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    from src.observability.setup import setup_telemetry
    setup_telemetry()  # must not raise


def test_setup_telemetry_no_op_sets_initialised_flag(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    import src.observability.setup as setup_mod
    setup_mod.setup_telemetry()
    assert setup_mod._INITIALISED is True


def test_setup_telemetry_installs_tracer_provider_on_no_op(monkeypatch):
    """Even with no endpoint, a TracerProvider is installed so get_tracer() works."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    from src.observability.setup import setup_telemetry, get_tracer
    setup_telemetry()
    tracer = get_tracer()
    assert tracer is not None


def test_setup_telemetry_installs_meter_provider_on_no_op(monkeypatch):
    """Even with no endpoint, a MeterProvider is installed so get_meter() works."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    from src.observability.setup import setup_telemetry, get_meter
    setup_telemetry()
    meter = get_meter()
    assert meter is not None


# ---------------------------------------------------------------------------
# setup_telemetry() — idempotency
# ---------------------------------------------------------------------------


def test_setup_telemetry_is_idempotent(monkeypatch):
    """Calling setup_telemetry() twice does not raise and only initialises once."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    import src.observability.setup as setup_mod
    setup_mod.setup_telemetry()
    setup_mod.setup_telemetry()  # second call — must be a no-op
    assert setup_mod._INITIALISED is True


def test_setup_telemetry_idempotent_with_endpoint(monkeypatch):
    """With an endpoint set, calling twice still only initialises once."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "123")
    monkeypatch.setenv("GRAFANA_OTLP_TOKEN", "test-token")

    import src.observability.setup as setup_mod
    # Patch exporters so no real network call is attempted
    from unittest.mock import patch, MagicMock
    with patch("src.observability.setup.OTLPSpanExporter", return_value=MagicMock()), \
         patch("src.observability.setup.OTLPMetricExporter", return_value=MagicMock()):
        setup_mod.setup_telemetry()
        setup_mod.setup_telemetry()  # second call
    assert setup_mod._INITIALISED is True


# ---------------------------------------------------------------------------
# _grafana_headers() — Basic Auth encoding
# ---------------------------------------------------------------------------


def test_grafana_headers_empty_when_no_credentials(monkeypatch):
    monkeypatch.delenv("GRAFANA_INSTANCE_ID", raising=False)
    monkeypatch.delenv("GRAFANA_OTLP_TOKEN", raising=False)
    from src.observability.setup import _grafana_headers
    assert _grafana_headers() == {}


def test_grafana_headers_empty_when_only_instance_id(monkeypatch):
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "123456")
    monkeypatch.delenv("GRAFANA_OTLP_TOKEN", raising=False)
    from src.observability.setup import _grafana_headers
    assert _grafana_headers() == {}


def test_grafana_headers_empty_when_only_token(monkeypatch):
    monkeypatch.delenv("GRAFANA_INSTANCE_ID", raising=False)
    monkeypatch.setenv("GRAFANA_OTLP_TOKEN", "glc_test_token")
    from src.observability.setup import _grafana_headers
    assert _grafana_headers() == {}


def test_grafana_headers_returns_authorization_key(monkeypatch):
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "123456")
    monkeypatch.setenv("GRAFANA_OTLP_TOKEN", "glc_test_token")
    from src.observability.setup import _grafana_headers
    headers = _grafana_headers()
    assert "Authorization" in headers


def test_grafana_headers_uses_basic_auth_scheme(monkeypatch):
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "123456")
    monkeypatch.setenv("GRAFANA_OTLP_TOKEN", "glc_test_token")
    from src.observability.setup import _grafana_headers
    headers = _grafana_headers()
    assert headers["Authorization"].startswith("Basic ")


def test_grafana_headers_base64_encodes_id_colon_token(monkeypatch):
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "123456")
    monkeypatch.setenv("GRAFANA_OTLP_TOKEN", "glc_test_token")
    from src.observability.setup import _grafana_headers
    headers = _grafana_headers()
    encoded = headers["Authorization"].split(" ", 1)[1]
    decoded = base64.b64decode(encoded).decode()
    assert decoded == "123456:glc_test_token"


def test_grafana_headers_instance_id_is_username(monkeypatch):
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "999888")
    monkeypatch.setenv("GRAFANA_OTLP_TOKEN", "some-token")
    from src.observability.setup import _grafana_headers
    headers = _grafana_headers()
    encoded = headers["Authorization"].split(" ", 1)[1]
    decoded = base64.b64decode(encoded).decode()
    instance_id, _ = decoded.split(":", 1)
    assert instance_id == "999888"


def test_grafana_headers_token_is_password(monkeypatch):
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "111")
    monkeypatch.setenv("GRAFANA_OTLP_TOKEN", "my-secret-token")
    from src.observability.setup import _grafana_headers
    headers = _grafana_headers()
    encoded = headers["Authorization"].split(" ", 1)[1]
    decoded = base64.b64decode(encoded).decode()
    _, token = decoded.split(":", 1)
    assert token == "my-secret-token"


# ---------------------------------------------------------------------------
# _build_resource() — OTel Resource attributes
# ---------------------------------------------------------------------------


def test_build_resource_service_name_from_env(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "my-test-service")
    from src.observability.setup import _build_resource
    resource = _build_resource()
    assert resource.attributes["service.name"] == "my-test-service"


def test_build_resource_service_name_default(monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    from src.observability.setup import _build_resource
    resource = _build_resource()
    assert resource.attributes["service.name"] == "modal-fastapi"


def test_build_resource_environment_from_env(monkeypatch):
    monkeypatch.setenv("MODAL_ENV", "prod")
    from src.observability.setup import _build_resource
    resource = _build_resource()
    assert resource.attributes["deployment.environment"] == "prod"


def test_build_resource_environment_default(monkeypatch):
    monkeypatch.delenv("MODAL_ENV", raising=False)
    from src.observability.setup import _build_resource
    resource = _build_resource()
    assert resource.attributes["deployment.environment"] == "dev"


def test_build_resource_version_from_env(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_VERSION", "2.5.0")
    from src.observability.setup import _build_resource
    resource = _build_resource()
    assert resource.attributes["service.version"] == "2.5.0"


def test_build_resource_version_default(monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_VERSION", raising=False)
    from src.observability.setup import _build_resource
    resource = _build_resource()
    assert resource.attributes["service.version"] == "1.0.0"


# ---------------------------------------------------------------------------
# setup_telemetry() — full path with mocked exporters
# ---------------------------------------------------------------------------


def test_setup_telemetry_with_endpoint_sets_initialised(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    import src.observability.setup as setup_mod
    from unittest.mock import patch, MagicMock
    with patch("src.observability.setup.OTLPSpanExporter", return_value=MagicMock()), \
         patch("src.observability.setup.OTLPMetricExporter", return_value=MagicMock()):
        setup_mod.setup_telemetry()
    assert setup_mod._INITIALISED is True


def test_setup_telemetry_span_exporter_gets_traces_path(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    import src.observability.setup as setup_mod
    from unittest.mock import patch, MagicMock, call
    mock_span_exporter_cls = MagicMock(return_value=MagicMock())
    with patch("src.observability.setup.OTLPSpanExporter", mock_span_exporter_cls), \
         patch("src.observability.setup.OTLPMetricExporter", return_value=MagicMock()):
        setup_mod.setup_telemetry()
    call_kwargs = mock_span_exporter_cls.call_args[1]
    assert call_kwargs["endpoint"] == "http://localhost:4318/v1/traces"


def test_setup_telemetry_metric_exporter_gets_metrics_path(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    import src.observability.setup as setup_mod
    from unittest.mock import patch, MagicMock
    mock_metric_exporter_cls = MagicMock(return_value=MagicMock())
    with patch("src.observability.setup.OTLPSpanExporter", return_value=MagicMock()), \
         patch("src.observability.setup.OTLPMetricExporter", mock_metric_exporter_cls):
        setup_mod.setup_telemetry()
    call_kwargs = mock_metric_exporter_cls.call_args[1]
    assert call_kwargs["endpoint"] == "http://localhost:4318/v1/metrics"


def test_setup_telemetry_passes_auth_headers_to_exporters(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("GRAFANA_INSTANCE_ID", "123456")
    monkeypatch.setenv("GRAFANA_OTLP_TOKEN", "glc_token")
    import src.observability.setup as setup_mod
    from unittest.mock import patch, MagicMock
    mock_span_cls = MagicMock(return_value=MagicMock())
    mock_metric_cls = MagicMock(return_value=MagicMock())
    with patch("src.observability.setup.OTLPSpanExporter", mock_span_cls), \
         patch("src.observability.setup.OTLPMetricExporter", mock_metric_cls):
        setup_mod.setup_telemetry()
    span_headers   = mock_span_cls.call_args[1]["headers"]
    metric_headers = mock_metric_cls.call_args[1]["headers"]
    assert "Authorization" in span_headers
    assert "Authorization" in metric_headers


def test_setup_telemetry_metric_export_interval_from_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "5000")
    import src.observability.setup as setup_mod
    from unittest.mock import patch, MagicMock
    mock_reader_cls = MagicMock(return_value=MagicMock())
    with patch("src.observability.setup.OTLPSpanExporter", return_value=MagicMock()), \
         patch("src.observability.setup.OTLPMetricExporter", return_value=MagicMock()), \
         patch("src.observability.setup.PeriodicExportingMetricReader", mock_reader_cls):
        setup_mod.setup_telemetry()
    _, kwargs = mock_reader_cls.call_args
    assert kwargs.get("export_interval_millis") == 5000


def test_setup_telemetry_metric_export_interval_default(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.delenv("OTEL_METRIC_EXPORT_INTERVAL_MS", raising=False)
    import src.observability.setup as setup_mod
    from unittest.mock import patch, MagicMock
    mock_reader_cls = MagicMock(return_value=MagicMock())
    with patch("src.observability.setup.OTLPSpanExporter", return_value=MagicMock()), \
         patch("src.observability.setup.OTLPMetricExporter", return_value=MagicMock()), \
         patch("src.observability.setup.PeriodicExportingMetricReader", mock_reader_cls):
        setup_mod.setup_telemetry()
    _, kwargs = mock_reader_cls.call_args
    assert kwargs.get("export_interval_millis") == 15000
