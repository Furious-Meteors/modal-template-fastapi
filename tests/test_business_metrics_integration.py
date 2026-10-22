"""
End to end: API request -> deps wiring -> metering sidecars -> global MeterProvider.

A subprocess is needed because OpenTelemetry providers are process-global.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run() -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "metrics_probe.py")],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(ROOT), "JWT_SECRET": "x" * 40},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_real_requests_produce_the_business_metrics():
    points = _run()

    def count(name, **labels):
        return sum(
            p["value"] for p in points
            if p["name"] == name and all(p["attrs"].get(k) == v for k, v in labels.items())
        )

    ops = lambda op, out: count("app.items.operations", operation=op, outcome=out)
    assert ops("create", "ok") == 1
    assert ops("create", "conflict") == 1       # the " PROBE " duplicate
    assert ops("get", "ok") == 1
    assert ops("get", "not_found") == 1         # /items/ghost
    assert ops("update", "ok") == 1
    assert ops("delete", "ok") == 1
    # each write published exactly one event: create, update, delete
    assert count("app.events.published", topic="items", outcome="ok") == 3
    assert count("app.events.published", topic="items", type="item.created") == 1


def test_units_and_label_sets_are_the_documented_ones():
    points = _run()
    units = {p["name"]: p["unit"] for p in points}
    assert units["app.items.operations"] == "1"
    assert units["app.items.operation.duration"] == "ms"
    assert units["app.events.publish.duration"] == "ms"
    label_keys = {p["name"]: set(p["attrs"]) for p in points}
    assert label_keys["app.items.operations"] == {"operation", "outcome"}
    assert label_keys["app.events.published"] == {"topic", "type", "outcome"}
    # nothing identifying (ids, names, users) ever becomes a label
    assert all(not ({"id", "item_id", "name", "user", "sub"} & set(p["attrs"])) for p in points)
