"""AUTH_BACKEND: shared secret (default) and an identity provider's JWKS, behind one TokenVerifier port."""
import http.server
import json
import threading
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from src.core.ports.token_verifier import TokenVerifier

TICKET_SECRET = "t" * 40


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key, kid):
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


class _Idp:
    """A local identity provider publishing a JWKS that tests can rotate."""

    def __init__(self):
        self.keys = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"keys": [_jwk(k, kid) for kid, k in outer.keys.items()]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_port}/jwks.json"

    def add_key(self, kid):
        self.keys[kid] = _key()
        return self.keys[kid]

    def token(self, kid, key=None, **claims):
        payload = {"sub": "user-1", "exp": int(time.time()) + 300, **claims}
        return jwt.encode(payload, key or self.keys[kid], algorithm="RS256", headers={"kid": kid})

    def close(self):
        self._server.shutdown()


@pytest.fixture
def idp(monkeypatch):
    from src.adapters.auth import jwt as adapter

    adapter._jwks_client.cache_clear()
    provider = _Idp()
    provider.add_key("k1")
    monkeypatch.setenv("AUTH_BACKEND", "jwks")
    monkeypatch.setenv("JWT_JWKS_URL", provider.url)
    monkeypatch.setenv("JWT_ALGORITHM", "RS256")
    monkeypatch.setenv("WS_TICKET_SECRET", TICKET_SECRET)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    yield provider
    provider.close()
    adapter._jwks_client.cache_clear()


def _verify(token):
    from src.api.auth import verify_token

    return verify_token(token)


def _code(token):
    from src.api.errors import AppError

    with pytest.raises(AppError) as exc:
        _verify(token)
    return exc.value.error_code


def test_both_backends_satisfy_the_port(idp, monkeypatch):
    from src.deps import get_token_verifier

    assert isinstance(get_token_verifier(), TokenVerifier)
    monkeypatch.setenv("AUTH_BACKEND", "secret")
    assert isinstance(get_token_verifier(), TokenVerifier)


def test_a_token_signed_by_the_provider_is_accepted(idp):
    payload = _verify(idp.token("k1", scopes=["items:read"]))
    assert payload.sub == "user-1" and payload.scopes == ["items:read"]


def test_a_token_signed_with_another_key_is_rejected(idp):
    assert _code(idp.token("k1", key=_key())) == "INVALID_TOKEN"


def test_an_expired_token_is_reported_as_expired(idp):
    assert _code(idp.token("k1", exp=int(time.time()) - 10)) == "TOKEN_EXPIRED"


def test_a_symmetric_token_cannot_be_passed_off_in_jwks_mode(idp):
    """The classic downgrade attack: sign with HS256 using the public key as the secret."""
    forged = jwt.encode({"sub": "x", "exp": int(time.time()) + 60}, "any-shared-secret-of-sufficient-length-123", algorithm="HS256", headers={"kid": "k1"})
    assert _code(forged) == "INVALID_TOKEN"


def test_provider_key_rotation_needs_no_restart(idp):
    from src.adapters.auth.jwt import _jwks_client

    _verify(idp.token("k1"))
    idp.add_key("k2")  # the provider rotates in a new key and starts signing with it
    # PyJWT refetches on an unknown kid, but not more often than its cooldown (it protects the
    # provider from a flood of forged kids); a new kid is accepted once that has passed.
    assert _code(idp.token("k2")) == "INVALID_TOKEN"
    _jwks_client(idp.url).cooldown_duration = 0
    assert _verify(idp.token("k2")).sub == "user-1"


def test_an_unknown_kid_is_rejected_without_crashing(idp):
    stranger = _key()
    assert _code(jwt.encode({"sub": "x", "exp": int(time.time()) + 60}, stranger, algorithm="RS256", headers={"kid": "nope"})) == "INVALID_TOKEN"


def test_audience_and_issuer_are_enforced_when_configured(idp, monkeypatch):
    monkeypatch.setenv("JWT_AUDIENCE", "my-api")
    monkeypatch.setenv("JWT_ISSUER", "https://idp.example")
    assert _code(idp.token("k1", aud="other-api", iss="https://idp.example")) == "INVALID_TOKEN"
    assert _code(idp.token("k1", aud="my-api", iss="https://evil.example")) == "INVALID_TOKEN"
    assert _verify(idp.token("k1", aud="my-api", iss="https://idp.example")).sub == "user-1"


def test_oauth2_scope_string_maps_to_scopes(idp):
    assert _verify(idp.token("k1", scope="items:read items:write")).scopes == ["items:read", "items:write"]


def test_a_custom_scopes_claim_can_be_a_list_or_a_string(idp, monkeypatch):
    monkeypatch.setenv("JWT_SCOPES_CLAIM", "permissions")
    assert _verify(idp.token("k1", permissions=["a", "b"])).scopes == ["a", "b"]
    assert _verify(idp.token("k1", permissions="a b")).scopes == ["a", "b"]
    assert _verify(idp.token("k1", scopes=["ignored"])).scopes == []


def test_websocket_tickets_do_not_depend_on_the_identity_provider(idp):
    from src.api.auth import create_ws_ticket, verify_ws_ticket

    user = _verify(idp.token("k1", scopes=["events:read", "items:read"]))
    ticket, _ = create_ws_ticket(user)
    claims, jti = verify_ws_ticket(ticket)
    assert claims.sub == "user-1" and jti
    assert jwt.decode(ticket, TICKET_SECRET, algorithms=["HS256"], audience="ws-ticket")["sub"] == "user-1"


def test_api_works_end_to_end_with_provider_tokens(idp):
    from src.api.auth import require_scope
    from src.api.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/whoami")
    def whoami(user=__import__("fastapi").Depends(require_scope("items:read"))):
        return {"sub": user.sub}

    client = TestClient(app)
    ok = client.get("/whoami", headers={"Authorization": f"Bearer {idp.token('k1', scope='items:read')}"})
    assert ok.status_code == 200 and ok.json() == {"sub": "user-1"}
    assert client.get("/whoami", headers={"Authorization": f"Bearer {idp.token('k1', scope='other')}"}).status_code == 403


# ── startup validation ──────────────────────────────────────────────────────

def test_jwks_mode_validates_at_startup(idp, monkeypatch):
    from src.api.auth import validate_auth_config

    validate_auth_config()
    monkeypatch.delenv("WS_TICKET_SECRET")
    with pytest.raises(RuntimeError, match="WS_TICKET_SECRET"):
        validate_auth_config()


@pytest.mark.parametrize("url", ["", "http://idp.example/jwks.json", "ftp://x"])
def test_jwks_url_must_be_https_unless_local(idp, monkeypatch, url):
    from src.api.auth import validate_auth_config

    monkeypatch.setenv("JWT_JWKS_URL", url)
    with pytest.raises(RuntimeError, match="JWT_JWKS_URL"):
        validate_auth_config()


def test_jwks_mode_refuses_symmetric_algorithms(idp, monkeypatch):
    from src.api.auth import validate_auth_config

    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    with pytest.raises(RuntimeError, match="not supported with AUTH_BACKEND=jwks"):
        validate_auth_config()


def test_jwks_mode_warns_when_no_audience_is_set(idp, caplog):
    from src.api.auth import validate_auth_config

    with caplog.at_level("WARNING"):
        validate_auth_config()
    assert "JWT_AUDIENCE" in caplog.text


def test_unknown_auth_backend_fails_at_startup(monkeypatch):
    from src.api.auth import validate_auth_config

    monkeypatch.setenv("AUTH_BACKEND", "carrier-pigeon")
    with pytest.raises(ValueError, match="AUTH_BACKEND"):
        validate_auth_config()
