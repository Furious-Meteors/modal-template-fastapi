"""
src/utils/dburl.py
==================
Describe and sanity-check Postgres/Supabase connection strings WITHOUT ever
exposing the password. Used by scripts/check_supabase.py and the test guard.

What it knows about Supabase (from Supabase's connection docs — verify against your
dashboard, as plans and hostnames change):
  • pooler hosts end in ".pooler.supabase.com"; port 6543 = transaction mode,
    port 5432 = session mode; the user is "postgres.<project-ref>"
  • direct hosts look like "db.<project-ref>.supabase.co"; they have historically
    been IPv6-only unless the IPv4 add-on is enabled
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
_PLACEHOLDERS = ("[YOUR-PASSWORD]", "YOUR-PASSWORD", "YOUR_PASSWORD", "<password>", "[PASSWORD]", "changeme")


@dataclass(frozen=True)
class UrlInfo:
    user: str
    host: str
    port: int | None
    database: str
    kind: str  # transaction-pooler | session-pooler | direct | local | other
    masked: str
    has_password: bool


# Supabase's dashboard shows "postgresql://postgres:[YOUR-PASSWORD]@host:port/db" —
# with literal square brackets, which a URL parser reads as an IPv6 address and
# rejects. People also paste it with the brackets still around their real password.
_BRACKETED_PASSWORD = re.compile(r"^[a-z]+://[^:/@]*:\[[^\]]*\]@", re.IGNORECASE)


def _safe_split(url: str):
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a bad port
        return parts
    except ValueError:
        return None


def is_local(url: str) -> bool:
    parts = _safe_split(url)
    return bool(parts) and (parts.hostname or "") in LOCAL_HOSTS


def mask_url(url: str) -> str:
    """
    The URL with the password replaced by ***. Safe to print — it never falls back
    to returning the original text when the URL cannot be parsed.
    """
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", url, re.IGNORECASE):
        # Not URL-shaped (e.g. someone pasted just the password): never echo it back.
        return "<not a URL>"
    parts = _safe_split(url)
    if parts is not None and parts.password is not None:
        host = parts.hostname or ""
        host = f"[{host}]" if ":" in host else host
        netloc = f"{parts.username or ''}:***@{host}" + (f":{parts.port}" if parts.port else "")
        return parts._replace(netloc=netloc).geturl()
    if parts is not None:
        return url  # parsed fine and has no password in it
    # Unparseable: mask everything between "scheme://user:" and the LAST "@".
    return re.sub(r"(://[^:/@]*:)(.*)(@[^@]*)$", r"\1***\3", url, count=1) if "@" in url else "<unparseable URL>"


def _classify(host: str, port: int | None) -> str:
    if host in LOCAL_HOSTS:
        return "local"
    if host.endswith(".pooler.supabase.com"):
        return "transaction-pooler" if port == 6543 else "session-pooler" if port in (5432, None) else "other"
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return "direct"
    return "other"


def describe_url(url: str) -> UrlInfo:
    parts = _safe_split(url)
    if parts is None:
        # Unparseable (typically an unreplaced [YOUR-PASSWORD]) — but the host and
        # port after the last "@" are still readable, so say what kind it is.
        tail = re.match(r"^[^@]*@([^:/?#]+)(?::(\d+))?", url.rsplit("@", 1)[0] + "@" + url.rsplit("@", 1)[-1]) if "@" in url else None
        host, port = (tail.group(1), int(tail.group(2)) if tail.group(2) else None) if tail else ("", None)
        return UrlInfo("", host, port, "", _classify(host, port), mask_url(url), "@" in url)
    host, port = parts.hostname or "", parts.port
    kind = _classify(host, port)
    return UrlInfo(
        user=parts.username or "",
        host=host,
        port=port,
        database=(parts.path or "/").lstrip("/"),
        kind=kind,
        masked=mask_url(url),
        has_password=bool(parts.password),
    )


def database_identity(url: str) -> tuple:
    """
    What database a URL really points at, so two different-looking strings for the
    same Supabase project compare equal (pooler user "postgres.<ref>" and direct
    host "db.<ref>.supabase.co" both carry the project ref).
    """
    info = describe_url(url)
    ref = None
    if info.kind in ("transaction-pooler", "session-pooler") and "." in info.user:
        ref = info.user.split(".", 1)[1]
    elif info.kind == "direct":
        ref = info.host.split(".")[1] if info.host.count(".") >= 2 else None
    if ref:
        return ("supabase", ref, info.database)
    return (info.host, info.database)


def url_problems(url: str, *, role: str) -> tuple[list[str], list[str]]:
    """
    (errors, warnings) for a connection string. `role` is "pool" (normal queries)
    or "listen" (the dedicated LISTEN connection, which needs a session).
    """
    errors: list[str] = []
    warnings: list[str] = []
    if any(p.lower() in url.lower() for p in _PLACEHOLDERS):
        return ["The password is still a placeholder (e.g. [YOUR-PASSWORD]) — replace it, brackets included."], []
    if _BRACKETED_PASSWORD.match(url):
        return [
            "The password is wrapped in [ ]. Supabase's template shows [YOUR-PASSWORD] — replace the whole "
            "thing, brackets included, with your real (URL-encoded) password."
        ], []
    parts = _safe_split(url)
    if parts is None:
        return ["The URL cannot be parsed (a password with @ : / ? # must be URL-encoded)."], []
    if url.count("@") > 1:
        errors.append("More than one '@': URL-encode special characters in the password (@ → %40, : → %3A, / → %2F, # → %23).")
    info = describe_url(url)
    if not parts.scheme.startswith("postgres"):
        errors.append("The URL must start with postgresql:// (or postgres://).")
    if not info.has_password and info.kind != "local":
        errors.append("The URL has no password.")
    if info.kind in ("transaction-pooler", "session-pooler") and "." not in info.user:
        errors.append(f"On a Supabase pooler the user must be 'postgres.<project-ref>' (got '{info.user or '(none)'}').")
    if "sslmode" in parse_qs(parts.query) and parse_qs(parts.query)["sslmode"] == ["disable"] and info.kind != "local":
        errors.append("sslmode=disable on a remote host: Supabase requires SSL.")
    if role == "listen":
        if info.kind == "transaction-pooler":
            errors.append(
                "LISTEN cannot work through the transaction pooler (port 6543). Use the session "
                "pooler (same host, port 5432) or the direct connection."
            )
    else:
        if info.kind == "direct":
            warnings.append(
                "Direct connection: per Supabase's docs it is IPv6-only unless you have the IPv4 add-on; "
                "if it will not connect, use the pooler string instead. Direct connections also do not "
                "scale across many containers."
            )
        elif info.kind == "session-pooler":
            warnings.append(
                "Session pooler (5432): works, but every container holds long-lived sessions. The transaction "
                "pooler (6543) is the better fit for DATABASE_URL."
            )
    return errors, warnings
