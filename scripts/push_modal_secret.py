#!/usr/bin/env python
"""
Create the Modal secret that carries the Supabase settings, straight from your
local .env — values are never printed and never appear on a command line (the
Modal CLI's `secret create KEY=VALUE` puts them in argv; this uses the SDK).

    python scripts/push_modal_secret.py --dry-run                     # show key NAMES only; contacts nothing
    python scripts/push_modal_secret.py --environment feat            # create `supabase-db` in Modal env "feat"
    python scripts/push_modal_secret.py --environment feat --replace  # rotate: delete and recreate it

--environment is REQUIRED for a real push. Modal secrets are scoped per environment
(dev / feat / prod here, matching `modal deploy --env $MODAL_ENV`), and the default is
whichever environment is active for your profile — often prod. Choosing implicitly would
be an easy way to put a database secret in the wrong place.

What goes into the secret (by default): PERSISTENCE_BACKEND, EVENT_BUS_BACKEND,
DATABASE_URL, EVENT_BUS_LISTEN_URL. Deliberately NOT pushed: JWT_SECRET and
webhook secrets (they live in their own Modal secrets), TEST_* settings, and
DB_POOL_MAX_SIZE (the .env value is a small local one; the app default of 5 matches
Modal's per-container concurrency — add it with --keys to override).

Other secrets use the same script with --name and --keys, e.g. the webhook provider secret:

    python scripts/push_modal_secret.py --environment feat --name webhook-secrets --keys WEBHOOK_SECRET_ACME

Authenticate first: `modal token new` (browser) or `modal token set`, or export
MODAL_TOKEN_ID / MODAL_TOKEN_SECRET. Then mount it by setting
`persistence_secret_name="supabase-db"` on an environment preset in modal_common.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.dburl import describe_url, url_problems  # noqa: E402
from src.utils.envfile import parse_env_text  # noqa: E402

DEFAULT_KEYS = ("PERSISTENCE_BACKEND", "EVENT_BUS_BACKEND", "DATABASE_URL", "EVENT_BUS_LISTEN_URL")
SECRET_LOOKING = ("SECRET", "TOKEN", "PASSWORD", "KEY")
URL_ROLES = {"DATABASE_URL": "pool", "EVENT_BUS_LISTEN_URL": "listen"}


class PushError(Exception):
    """A problem the user can fix; the message is safe to print."""


def build_payload(env: dict[str, str], keys: tuple[str, ...] = DEFAULT_KEYS) -> dict[str, str]:
    """Pick and validate the secret's contents from a parsed .env."""
    payload = {k: env[k] for k in keys if env.get(k)}
    missing = [k for k in keys if not env.get(k) and k not in ("EVENT_BUS_LISTEN_URL",)]
    if keys == DEFAULT_KEYS and "DATABASE_URL" not in payload:
        raise PushError("DATABASE_URL is not set in the env file.")
    if keys != DEFAULT_KEYS and missing:
        raise PushError("Not set in the env file: " + ", ".join(missing))
    if not payload:
        raise PushError("Nothing to push: none of the requested variables is set in the env file.")
    problems: list[str] = []
    for key, role in URL_ROLES.items():
        if key in payload:
            errors, _ = url_problems(payload[key], role=role)
            problems += [f"{key}: {e}" for e in errors]
    if problems:
        raise PushError("Refusing to push an invalid connection string:\n  - " + "\n  - ".join(problems))
    if payload.get("PERSISTENCE_BACKEND") == "memory":
        raise PushError("PERSISTENCE_BACKEND=memory — nothing to push (set it to postgres).")
    if "DATABASE_URL" in payload and payload.get("EVENT_BUS_BACKEND") == "postgres" and "EVENT_BUS_LISTEN_URL" not in payload:
        # Not an error: the bus polls without it. Just worth saying.
        print("note: EVENT_BUS_LISTEN_URL is not set — events will be picked up by polling.")
    return payload


def describe(payload: dict[str, str]) -> list[str]:
    """Key names (and masked URLs) — never values."""
    lines = []
    for key in sorted(payload):
        if key in URL_ROLES:
            info = describe_url(payload[key])
            lines.append(f"  {key} = {info.masked}   [{info.kind}]")
        elif any(w in key.upper() for w in SECRET_LOOKING):
            lines.append(f"  {key} = <hidden, {len(payload[key])} characters>")
        else:
            lines.append(f"  {key} = {payload[key]}")
    return lines


def push(
    payload: dict[str, str],
    name: str,
    *,
    replace: bool,
    environment: Optional[str],
    secrets_api: Any,
) -> str:
    """Create (or, with replace, recreate) the secret. Returns 'created' or 'replaced'."""
    existing = {s.name for s in secrets_api.list(environment_name=environment or "")}
    if name in existing and not replace:
        raise PushError(
            f"A Modal secret named '{name}' already exists. Run again with --replace to overwrite it "
            "(containers started during the swap can fail to find it, so do it between deploys)."
        )
    action = "replaced" if name in existing else "created"
    if name in existing:
        secrets_api.delete(name, allow_missing=True, environment_name=environment)
    secrets_api.create(name, payload, environment_name=environment)
    return action


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--name", default="supabase-db", help="Modal secret name (default: supabase-db)")
    parser.add_argument("--keys", default=",".join(DEFAULT_KEYS), help="comma-separated variables to include")
    parser.add_argument("--environment", default=None, help="Modal environment to create the secret in (dev|feat|prod); required")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing secret")
    parser.add_argument("--dry-run", action="store_true", help="validate and show key names; contact nothing")
    args = parser.parse_args(argv)

    path = Path(args.env_file)
    if not path.is_file():
        print(f"No {path}. Create it first:  cp .env.example .env", file=sys.stderr)
        return 2
    try:
        payload = build_payload(parse_env_text(path.read_text()), tuple(k.strip() for k in args.keys.split(",") if k.strip()))
    except PushError as exc:
        print(exc, file=sys.stderr)
        return 1

    target = f" in Modal environment '{args.environment}'" if args.environment else ""
    print(f"Secret '{args.name}'{target} would contain {len(payload)} variable(s):")
    print("\n".join(describe(payload)))
    if args.dry_run:
        print("Dry run: nothing was sent to Modal.")
        return 0
    if not args.environment:
        print("Refusing to guess the Modal environment. Pass --environment feat (or dev / prod): secrets are "
              "scoped per environment and the default is whichever is active for your profile.", file=sys.stderr)
        return 2

    try:
        import modal

        action = push(payload, args.name, replace=args.replace, environment=args.environment, secrets_api=modal.Secret.objects)
    except PushError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:  # auth problems etc.: class name and first line only, never values
        first = (str(exc).splitlines() or [""])[0][:200]
        print(f"Could not reach Modal: {type(exc).__name__}: {first}", file=sys.stderr)
        print("Authenticate first:  modal token new   (or: modal token set)", file=sys.stderr)
        return 3
    print(f"Modal secret '{args.name}' {action}. Mount it with persistence_secret_name=\"{args.name}\" in modal_common.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
