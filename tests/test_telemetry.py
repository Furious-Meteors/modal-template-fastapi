"""
FastAPI's native telemetry must not duplicate TelemetryMiddleware's.

Run in a subprocess because OpenTelemetry providers are process-global and a
real exporter pipeline is needed to see what actually gets emitted.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _probe(**env) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "telemetry_probe.py")],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT), "JWT_SECRET": "x" * 40, **env},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_native_telemetry_is_off_by_default():
    from src.main import app

    assert app._telemetry["tracing"] is False
    assert app._telemetry["metrics"] is False
    assert app._telemetry["logs"] is False


def test_exactly_one_server_span_per_request():
    data = _probe()
    server = [s["name"] for s in data["spans"] if s["kind"] == "SERVER"]
    assert sorted(server) == ["GET /api/v1/health", "GET unmatched"]  # no bare "GET", no duplicates


def test_no_clashing_metric_names_or_units():
    names = [n for n, _ in _probe()["metrics"]]
    assert len(names) == len(set(names)), f"same metric name emitted with different units: {names}"
    units = dict(_probe()["metrics"])
    assert units["http.server.request.duration"] == "ms"
    assert units["http.server.active_requests"] == "1"


def test_native_telemetry_can_be_restored_with_the_env_flag():
    data = _probe(FASTAPI_NATIVE_TELEMETRY="true")
    server = [s for s in data["spans"] if s["kind"] == "SERVER"]
    assert len(server) > 2  # FastAPI's own server spans are back (and duplicate ours)
    assert any(s["name"].startswith("fastapi.") for s in data["spans"])
