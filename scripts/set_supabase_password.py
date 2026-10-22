#!/usr/bin/env python
"""
Put your Supabase database password into .env without it ever being echoed, logged
or passing through a chat: it prompts (hidden), URL-encodes the password, and
replaces every [YOUR-PASSWORD] placeholder in the file.

    python scripts/set_supabase_password.py              # prompts twice, no echo
    python scripts/set_supabase_password.py --stdin      # read one line from stdin (scripting/tests)
    python scripts/set_supabase_password.py --replace    # rotate: replace a password that is already set

Encoding matters: a password containing @ : / ? # % breaks a connection URL unless
it is percent-encoded, and that is the most common reason a Supabase URL "does not work".
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "[YOUR-PASSWORD]"

# "KEY=postgresql://user:<password>@<host>..." where <host> is a Supabase host; the
# password is everything up to the LAST "@" on the line. Commented-out lines count.
_SUPABASE_URL_LINE = re.compile(
    r"^(?P<head>\s*#?\s*[A-Za-z_][A-Za-z0-9_]*=\s*postgres(?:ql)?://[^:/@\s]+:)"
    r"(?P<password>[^\s]*)"
    r"(?P<tail>@[^@\s/?#]*(?:supabase\.com|supabase\.co)\b[^@\s]*\s*)$"
)


def apply_password(text: str, password: str, *, replace_existing: bool = False) -> tuple[str, int]:
    """
    Return (new_text, number_of_lines_changed). The password is URL-encoded.

    By default only [YOUR-PASSWORD] placeholders are filled. With replace_existing,
    the password in every Supabase connection URL is replaced as well (for rotating
    it); URLs for other hosts are never touched.
    """
    encoded = quote(password, safe="")
    lines, changed = [], 0
    for line in text.splitlines(keepends=True):
        if PLACEHOLDER in line:
            line = line.replace(PLACEHOLDER, encoded)
            changed += 1
        elif replace_existing:
            newline = "\n" if line.endswith("\n") else ""
            m = _SUPABASE_URL_LINE.match(line.rstrip("\n"))
            if m:
                line = f"{m['head']}{encoded}{m['tail']}{newline}"
                changed += 1
        lines.append(line)
    return "".join(lines), changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--stdin", action="store_true", help="read the password from one line of stdin")
    parser.add_argument("--replace", action="store_true",
                        help="also replace a password that is already set (rotating it) in every Supabase URL")
    args = parser.parse_args()

    path = Path(args.env_file)
    if not path.is_file():
        print(f"No {path}. Create it first:  cp .env.example .env", file=sys.stderr)
        return 2
    text = path.read_text()
    if PLACEHOLDER not in text and not args.replace:
        print(f"No {PLACEHOLDER} placeholder left in {path.name} — the password is already set. "
              "To change it, run again with --replace.", file=sys.stderr)
        return 1

    if args.stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    elif not sys.stdin.isatty():
        print("Standard input is not a terminal, so a hidden prompt is impossible. Run it in a terminal, "
              "or pipe the password with --stdin.", file=sys.stderr)
        return 2
    else:
        password = getpass.getpass("Supabase database password (hidden): ")
        if getpass.getpass("Again to confirm: ") != password:
            print("The two entries differ; nothing was changed.", file=sys.stderr)
            return 1
    if not password:
        print("Empty password; nothing was changed.", file=sys.stderr)
        return 1

    new_text, changed = apply_password(text, password, replace_existing=args.replace)
    if changed == 0:
        print("No Supabase connection URL found to update; nothing was changed.", file=sys.stderr)
        return 1
    # Write atomically and keep the file private to this user.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"Updated {changed} line(s) in {path.name} (password stored URL-encoded; not shown). "
          "Next: python scripts/check_supabase.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
