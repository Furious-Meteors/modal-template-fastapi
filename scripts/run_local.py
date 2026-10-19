#!/usr/bin/env python
"""
Run the API locally with the settings from .env (git-ignored).

    python scripts/run_local.py            # http://127.0.0.1:8000
    python scripts/run_local.py --reload   # restart on code changes

Only the app's variables are loaded; TEST_* variables are left to pytest. An
`export` in your shell always wins over the file. Passwords are never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.dburl import describe_url, url_problems  # noqa: E402
from src.utils.envfile import load_env_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if not Path(args.env_file).is_file():
        print(f"No {args.env_file}. Create it:  cp .env.example .env  (then fill it in)", file=sys.stderr)
        return 2
    load_env_file(args.env_file, exclude_prefixes=("TEST_", "ALLOW_DESTRUCTIVE_"))
    os.environ.setdefault("MODAL_ENV", "dev")

    persistence = os.environ.get("PERSISTENCE_BACKEND", "memory")
    bus = os.environ.get("EVENT_BUS_BACKEND", "memory")
    print(f"persistence={persistence}  event bus={bus}  env={os.environ['MODAL_ENV']}")
    problems = False
    for name, role in (("DATABASE_URL", "pool"), ("EVENT_BUS_LISTEN_URL", "listen")):
        url = os.environ.get(name)
        if not url:
            continue
        info = describe_url(url)
        print(f"{name} → {info.masked}  [{info.kind}]")
        errors, warnings = url_problems(url, role=role)
        for message in warnings:
            print(f"  warning: {message}")
        for message in errors:
            print(f"  ERROR: {message}", file=sys.stderr)
            problems = True
    if problems:
        print("Fix the errors above (python scripts/check_supabase.py explains each one).", file=sys.stderr)
        return 2
    if persistence == "postgres" and not os.environ.get("DATABASE_URL"):
        print("PERSISTENCE_BACKEND=postgres needs DATABASE_URL.", file=sys.stderr)
        return 2

    print(f"Serving on http://{args.host}:{args.port}  (docs at /docs). Token: python scripts/jwt_token_generator.py")
    import uvicorn

    uvicorn.run("src.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
