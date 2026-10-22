"""
src/utils/envfile.py
====================
A tiny .env reader for LOCAL use (scripts/run_local.py, scripts/check_supabase.py,
tests). It is never imported by the app itself and is not used in Modal, where
configuration comes from Modal Secrets.

No dependency, deliberately simple: KEY=VALUE lines, `export ` prefix, # comments,
single or double quoted values. Values are never logged or returned by
load_env_file — only the names of the variables that were set.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _KEY.match(key):
            continue
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # inline comment
        values[key] = value
    return values


def load_env_file(
    path: str | os.PathLike,
    *,
    include_prefixes: Optional[Iterable[str]] = None,
    exclude_prefixes: Iterable[str] = (),
    override: bool = False,
) -> list[str]:
    """
    Set variables from `path` into os.environ and return the NAMES that were set.

    include_prefixes: if given, only variables starting with one of these are read.
    exclude_prefixes: variables starting with one of these are skipped.
    override: by default a variable already in the environment wins, so a real
              `export` always beats the file.
    """
    p = Path(path)
    if not p.is_file():
        return []
    include = tuple(include_prefixes) if include_prefixes is not None else None
    exclude = tuple(exclude_prefixes)
    applied: list[str] = []
    for key, value in parse_env_text(p.read_text()).items():
        if include is not None and not key.startswith(include):
            continue
        if key.startswith(exclude):
            continue
        if key in os.environ and not override:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied
