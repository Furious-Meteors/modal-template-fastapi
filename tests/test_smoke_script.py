"""scripts/smoke_test.py: it passes against a healthy app, and it can genuinely fail."""
import importlib.util
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from tests.conftest import TEST_SECRET

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("smoke_test", ROOT / "scripts" / "smoke_test.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

HOOK_SECRET = "smoke-test-webhook-secret-value"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve(app):
    """Run an ASGI app on a real socket in a thread, so the script talks to it over real HTTP/WS."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="websockets-sansio"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "test server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def app_url(monkeypatch):
    from src.main import app

    monkeypatch.setenv("WEBHOOK_SECRET_ACME", HOOK_SECRET)
    with serve(app) as url:
        yield url


def run(args, capsys):
    code = smoke.main(args)
    return code, capsys.readouterr()


def test_a_healthy_app_passes_every_check(app_url, capsys):
    code, out = run(["--url", app_url, "--jwt-secret", TEST_SECRET, "--webhook-secret", HOOK_SECRET, "--concurrency", "12"], capsys)
    assert code == 0, out.out
    assert "0 failed" in out.out and "FAIL" not in out.out
    for expected in (
        "health is healthy", "create item -> 201", "duplicate name (case/space) -> 409",
        "read-only token cannot write -> 403", "12 concurrent creates of one name: exactly one 201",
        "SSE: an item created by another request arrives", "ws receives the item event",
        "ws ticket cannot be replayed", "signed webhook accepted -> 202",
        "20 simultaneous deliveries of one id: exactly one accepted",
    ):
        assert f"PASS {expected}" in out.out, f"missing: {expected}\n{out.out}"


def test_it_cleans_up_the_items_it_created(app_url, capsys):
    import httpx

    from tests.conftest import make_token

    headers = {"Authorization": "Bearer " + make_token(scopes=["items:read", "items:write"])}
    before = httpx.get(f"{app_url}/api/v1/items?limit=500", headers=headers).json()["total"]
    code, _ = run(["--url", app_url, "--jwt-secret", TEST_SECRET, "--concurrency", "6", "--skip", "ws,sse"], capsys)
    assert code == 0
    after = httpx.get(f"{app_url}/api/v1/items?limit=500", headers=headers).json()
    assert after["total"] == before
    assert not [i for i in after["items"] if i["name"].startswith("smoke-")]


def test_webhook_checks_are_skipped_without_a_secret_but_the_unknown_provider_check_still_runs(app_url, capsys):
    code, out = run(["--url", app_url, "--jwt-secret", TEST_SECRET, "--only", "webhooks"], capsys)
    assert code == 0
    assert "PASS webhook for an unknown provider -> 404" in out.out
    assert "SKIP signed webhook checks" in out.out


def test_scope_checks_are_skipped_when_only_one_token_is_given(app_url, capsys):
    from tests.conftest import make_token

    token = make_token(scopes=["events:read", "items:read", "items:write", "webhooks:read"])
    code, out = run(["--url", app_url, "--token", token, "--only", "scopes"], capsys)
    assert code == 0 and "SKIP scope checks" in out.out


def test_only_and_skip_select_groups(app_url, capsys):
    code, out = run(["--url", app_url, "--jwt-secret", TEST_SECRET, "--only", "basic"], capsys)
    assert code == 0 and "create item" in out.out and "concurrent creates" not in out.out and "ws " not in out.out


# ── it can fail ─────────────────────────────────────────────────────────────

def test_the_wrong_credentials_fail_the_run(app_url, capsys):
    code, out = run(["--url", app_url, "--jwt-secret", "a-completely-different-secret-of-32-characters", "--only", "basic"], capsys)
    assert code == 1
    assert "FAIL create item -> 201" in out.out


def test_a_deployment_that_breaks_uniqueness_is_caught(capsys):
    """A stub that accepts every create: the race check must notice more than one 201."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    counter = iter(range(10_000))

    async def create(request):
        return JSONResponse({"data": {"id": str(next(counter)), "name": "x", "description": None}}, status_code=201)

    async def delete(request):
        return Response(status_code=204)

    stub = Starlette(routes=[Route("/api/v1/items", create, methods=["POST"]),
                             Route("/api/v1/items/{item_id}", delete, methods=["DELETE"])])
    with serve(stub) as url:
        code, out = run(["--url", url, "--jwt-secret", TEST_SECRET, "--only", "race", "--concurrency", "8"], capsys)
    assert code == 1
    assert "FAIL 8 concurrent creates of one name: exactly one 201" in out.out
    assert "201x8" in out.out


def test_an_unreachable_deployment_fails_loudly_not_silently(capsys):
    port = _free_port()  # nothing is listening
    with pytest.raises(Exception):
        smoke.main(["--url", f"http://127.0.0.1:{port}", "--jwt-secret", TEST_SECRET, "--only", "basic"])


# ── usage and safety ────────────────────────────────────────────────────────

def test_writing_to_a_remote_host_needs_explicit_confirmation(capsys):
    code, out = run(["--url", "https://example.invalid", "--jwt-secret", TEST_SECRET], capsys)
    assert code == 2 and "--yes" in out.err and "WRITES" in out.err


def test_it_demands_a_url_and_credentials(capsys, monkeypatch):
    for var in ("SMOKE_URL", "SMOKE_TOKEN", "SMOKE_JWT_SECRET", "JWT_SECRET", "SMOKE_LIMITED_TOKEN", "SMOKE_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    assert run([], capsys)[0] == 2
    code, out = run(["--url", "http://127.0.0.1:1"], capsys)
    assert code == 2 and "--token" in out.err


def test_tokens_and_secrets_are_never_printed(app_url, capsys):
    code, out = run(["--url", app_url, "--jwt-secret", TEST_SECRET, "--webhook-secret", HOOK_SECRET, "--skip", "ws,sse,race"], capsys)
    assert code == 0
    assert TEST_SECRET not in out.out + out.err and HOOK_SECRET not in out.out + out.err
    assert "eyJ" not in out.out + out.err  # no JWT either
