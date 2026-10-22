"""
Mint SHORT-LIVED test tokens inside Modal, using the deployment's own auth secret, so you
can smoke-test a Modal environment whose JWT_SECRET you cannot read. The secret never leaves
Modal: only the tokens (and the secret's length) come back.

    modal run --env feat scripts/modal_mint_token.py --out /tmp/tokens.json
    python scripts/smoke_test.py --url https://... \\
        --token "$(python -c 'import json;print(json.load(open("/tmp/tokens.json"))["full"])')" \\
        --limited-token "$(python -c 'import json;print(json.load(open("/tmp/tokens.json"))["limited"])')" --yes

Tokens expire after --ttl seconds (default 900) and are written to a private (0600) file,
never printed. Delete the file when you are done.
"""
import json
import os

import modal

app = modal.App("mint-test-token")
image = modal.Image.debian_slim(python_version="3.11").pip_install("PyJWT==2.15.1")


@app.function(image=image, secrets=[modal.Secret.from_name("fastapi-auth-secrets")])
def mint(ttl: int = 900) -> dict:
    import time

    import jwt

    key = os.environ.get("JWT_SECRET") or ""
    alg = os.environ.get("JWT_ALGORITHM", "HS256")
    now = int(time.time())
    extra = {}
    if os.environ.get("JWT_AUDIENCE"): extra["aud"] = os.environ["JWT_AUDIENCE"]
    if os.environ.get("JWT_ISSUER"): extra["iss"] = os.environ["JWT_ISSUER"]

    def tok(sub, scopes):
        return jwt.encode({"sub": sub, "iat": now, "exp": now + ttl, "scopes": scopes, **extra}, key, algorithm=alg)

    return {
        "info": {"jwt_secret_length": len(key), "algorithm": alg, "meets_app_minimum_32": len(key) >= 32},
        "full": tok("smoke-test", ["events:read", "items:read", "items:write", "webhooks:read"]),
        "limited": tok("smoke-limited", ["events:read", "items:read"]),
    }


@app.local_entrypoint()
def main(out: str, ttl: int = 900):
    result = mint.remote(ttl)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"full": result["full"], "limited": result["limited"]}, f)
    print("info:", json.dumps(result["info"]))
    print("tokens written to", out, "(not printed)")
