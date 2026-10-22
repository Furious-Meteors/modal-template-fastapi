"""
Contract for route labels on spans and metrics.

TelemetryMiddleware derives the label from scope["route"], which is a FastAPI /
Starlette internal. These tests pin the observable behaviour so an upgrade that
changes it fails here, loudly, rather than silently corrupting dashboards with
raw URLs (unbounded label cardinality) or doubled prefixes.
"""
import re
from importlib import metadata
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from src.adapters.http import middleware

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def labels(monkeypatch):
    seen: list[str] = []
    real = middleware._route_template

    def spy(conn):
        label = real(conn)
        seen.append(label)
        return label

    monkeypatch.setattr(middleware, "_route_template", spy)
    return seen


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/v1/health", "/api/v1/health"),
    ("GET", "/api/v1/items/abc-123", "/api/v1/items/{item_id}"),
    ("PUT", "/api/v1/items/zzz", "/api/v1/items/{item_id}"),
    ("DELETE", "/api/v1/items/zzz", "/api/v1/items/{item_id}"),
    ("POST", "/api/v1/webhooks/nobody", "/api/v1/webhooks/{provider}"),
    ("POST", "/api/v1/ws/ticket", "/api/v1/ws/ticket"),
    ("GET", "/docs", "/docs"),                                  # plain Starlette route
    ("GET", "/openapi.json", "/openapi.json"),
    ("GET", "/definitely/not/here", middleware.UNMATCHED_ROUTE),
    ("GET", "/api/v1/", middleware.UNMATCHED_ROUTE),
    ("GET", "/api/v1/items/a/b/c", middleware.UNMATCHED_ROUTE),
])
def test_http_route_labels(client, labels, method, path, expected):
    client.request(method, path)
    assert labels == [expected]


def test_wrong_method_on_a_real_path_uses_the_route_not_the_raw_url(client, labels):
    assert client.patch("/api/v1/items/abc").status_code == 405
    assert labels == ["/api/v1/items/{item_id}"]


def test_many_distinct_urls_never_create_many_labels(client, labels):
    for i in range(50):
        client.get(f"/api/v1/items/id-{i}")
        client.get(f"/scan/{i}")
    assert set(labels) == {"/api/v1/items/{item_id}", middleware.UNMATCHED_ROUTE}


def test_websocket_label_has_exactly_one_prefix(client, labels):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.receive_json()
    assert labels == ["/api/v1/ws"]


def test_a_broken_route_lookup_never_fails_the_request(client, monkeypatch):
    def boom(conn):
        raise RuntimeError("scope layout changed")

    monkeypatch.setattr(middleware, "_route_template", boom)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert "x-session-id" in r.headers


def test_safe_route_degrades_to_unmatched(monkeypatch):
    def boom(conn):
        raise KeyError("route")

    monkeypatch.setattr(middleware, "_route_template", boom)
    assert middleware._safe_route(object()) == middleware.UNMATCHED_ROUTE


# ── the pins that the label logic was verified against ──────────────────────

def _pins(source: str) -> dict[str, str]:
    return {
        re.sub(r"\[.*\]", "", name).strip().lower().replace("_", "-"): version
        for name, version in re.findall(r'"([A-Za-z0-9_.\-\[\]]+)==([^"]+)"', source)
    }


def test_installed_runtime_packages_match_the_deploy_pins():
    """Tests must run against what is deployed (modal_common.PIP_PACKAGES)."""
    pins = _pins((ROOT / "modal_common.py").read_text())
    assert "fastapi" in pins, "could not parse pins from modal_common.py"
    mismatched = {}
    for name, wanted in pins.items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        if installed != wanted:
            mismatched[name] = f"installed {installed}, pinned {wanted}"
    assert not mismatched, (
        f"{mismatched} — bump the pin in modal_common.py and .github/requirements/test.txt "
        "together, and re-check tests/test_route_labels.py"
    )


def test_ci_test_requirements_use_the_same_pins_as_the_deploy_image():
    deploy = _pins((ROOT / "modal_common.py").read_text())
    lines = (ROOT / ".github/requirements/test.txt").read_text().splitlines()
    ci = _pins("\n".join(f'"{line.strip()}"' for line in lines if "==" in line))
    drift = {k: (v, deploy[k]) for k, v in ci.items() if k in deploy and deploy[k] != v}
    assert not drift, f"CI pins differ from deploy pins (ci, deploy): {drift}"
