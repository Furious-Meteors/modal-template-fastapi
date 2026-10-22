"""
The local Supabase testing setup: .env loading, connection-string diagnostics,
the guard against dropping a remote database's tables, the scripts, and keeping
credentials out of git and out of the Modal image.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.utils.dburl import database_identity, describe_url, is_local, mask_url, url_problems
from src.utils.envfile import load_env_file, parse_env_text
from tests.conftest import ENV_FILE_PREFIXES, TEST_DATABASE_URL, requires_postgres

ROOT = Path(__file__).resolve().parent.parent
SECRET = "S3cr3t-Pa55word"
POOLER = f"postgresql://postgres.abcd:{SECRET}@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"


# ── .env parsing and loading ────────────────────────────────────────────────

def test_parser_handles_comments_exports_quotes_and_urls():
    env = parse_env_text(
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        "export EXPORTED=yes\n"
        'DOUBLE="has spaces # not a comment"\n'
        "SINGLE='quoted'\n"
        "INLINE=value # trailing comment\n"
        "URL=postgresql://u:p%40ss@host:6543/db?sslmode=require&x=1#frag\n"
        "EMPTY=\n"
        "not a valid line\n"
        "1BAD=nope\n"
    )
    assert env == {
        "PLAIN": "value", "EXPORTED": "yes", "DOUBLE": "has spaces # not a comment",
        "SINGLE": "quoted", "INLINE": "value",
        "URL": "postgresql://u:p%40ss@host:6543/db?sslmode=require&x=1#frag",  # '=' and '#' survive
        "EMPTY": "",
    }


def test_loading_returns_names_never_values_and_the_shell_wins(tmp_path, monkeypatch):
    f = tmp_path / "e.env"
    f.write_text(f"NEW_VAR_1=from-file\nEXISTING_VAR_1=from-file\nSECRETY={SECRET}\n")
    monkeypatch.delenv("NEW_VAR_1", raising=False)
    monkeypatch.delenv("SECRETY", raising=False)
    monkeypatch.setenv("EXISTING_VAR_1", "from-shell")
    applied = load_env_file(f)
    assert sorted(applied) == ["NEW_VAR_1", "SECRETY"]
    assert os.environ["NEW_VAR_1"] == "from-file"
    assert os.environ["EXISTING_VAR_1"] == "from-shell"  # a real export beats the file
    assert SECRET not in "".join(applied)
    monkeypatch.delenv("NEW_VAR_1"); monkeypatch.delenv("SECRETY")


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == []


def test_prefix_filters(tmp_path, monkeypatch):
    f = tmp_path / "e.env"
    f.write_text("TEST_A=1\nAPP_B=2\nDB_C=3\n")
    for k in ("TEST_A", "APP_B", "DB_C"):
        monkeypatch.delenv(k, raising=False)
    assert load_env_file(f, include_prefixes=("TEST_",)) == ["TEST_A"]
    assert load_env_file(f, exclude_prefixes=("TEST_",)) == ["APP_B", "DB_C"]
    for k in ("TEST_A", "APP_B", "DB_C"):
        monkeypatch.delenv(k, raising=False)


def test_pytest_only_reads_test_settings_from_dotenv(tmp_path, monkeypatch):
    """Loading the app's own DATABASE_URL would make every TestClient connect to a real database."""
    f = tmp_path / ".env"
    f.write_text(
        "DATABASE_URL=postgresql://x:y@real.example.com/db\nPERSISTENCE_BACKEND=postgres\n"
        "EVENT_BUS_BACKEND=postgres\nJWT_SECRET=nope\nTEST_DATABASE_URL=postgresql://a:b@localhost/t\n"
        "ALLOW_DESTRUCTIVE_DB_TESTS=yes\nDB_POOL_MAX_SIZE=2\n"
    )
    for k in ("DATABASE_URL", "PERSISTENCE_BACKEND", "EVENT_BUS_BACKEND", "TEST_DATABASE_URL",
              "ALLOW_DESTRUCTIVE_DB_TESTS", "DB_POOL_MAX_SIZE"):
        monkeypatch.delenv(k, raising=False)
    saved_jwt = os.environ["JWT_SECRET"]
    applied = load_env_file(f, include_prefixes=ENV_FILE_PREFIXES)
    assert sorted(applied) == ["ALLOW_DESTRUCTIVE_DB_TESTS", "DB_POOL_MAX_SIZE", "TEST_DATABASE_URL"]
    assert "DATABASE_URL" not in os.environ and "PERSISTENCE_BACKEND" not in os.environ
    assert os.environ["JWT_SECRET"] == saved_jwt
    for k in applied:
        monkeypatch.delenv(k, raising=False)


# ── connection-string diagnostics ───────────────────────────────────────────

def test_masking_hides_the_password_and_classifies_the_connection():
    assert mask_url(POOLER) == "postgresql://postgres.abcd:***@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
    assert describe_url(POOLER).kind == "transaction-pooler"
    assert describe_url(POOLER.replace(":6543", ":5432")).kind == "session-pooler"
    assert describe_url(f"postgresql://postgres:{SECRET}@db.abcd.supabase.co:5432/postgres").kind == "direct"
    assert describe_url("postgresql://postgres:pw@localhost:5432/db").kind == "local"


@pytest.mark.parametrize("url,fragment", [
    ("postgresql://postgres.abcd:[YOUR-PASSWORD]@h.pooler.supabase.com:6543/postgres", "placeholder"),
    (f"postgresql://postgres.abcd:[{SECRET}]@h.pooler.supabase.com:6543/postgres", "wrapped in [ ]"),
    (f"postgresql://postgres.abcd:p@ss{SECRET}@h.pooler.supabase.com:6543/postgres", "URL-encode"),
    (f"postgresql://postgres:{SECRET}@h.pooler.supabase.com:6543/postgres", "postgres.<project-ref>"),
    ("postgresql://postgres.abcd@h.pooler.supabase.com:6543/postgres", "no password"),
    (f"postgresql://postgres.abcd:{SECRET}@h.pooler.supabase.com:6543/postgres?sslmode=disable", "sslmode=disable"),
    (f"mysql://u:{SECRET}@h/db", "postgresql://"),
])
def test_common_mistakes_get_a_specific_error(url, fragment):
    errors, _ = url_problems(url, role="pool")
    assert any(fragment in e for e in errors), errors


def test_a_good_url_has_no_errors_and_direct_or_session_pooler_get_advice():
    assert url_problems(POOLER, role="pool") == ([], [])
    direct = f"postgresql://postgres:{SECRET}@db.abcd.supabase.co:5432/postgres"
    assert url_problems(direct, role="pool")[0] == []
    assert any("IPv6" in w for w in url_problems(direct, role="pool")[1])
    assert any("transaction" in w for w in url_problems(POOLER.replace(":6543", ":5432"), role="pool")[1])


def test_listen_through_the_transaction_pooler_is_an_error_but_session_is_fine():
    assert any("LISTEN" in e for e in url_problems(POOLER, role="listen")[0])
    assert url_problems(POOLER.replace(":6543", ":5432"), role="listen") == ([], [])


def test_the_same_supabase_project_is_recognised_across_url_styles():
    direct = "postgresql://postgres:pw@db.abcd.supabase.co:5432/postgres"
    assert database_identity(POOLER) == database_identity(direct)
    assert database_identity(POOLER) != database_identity(POOLER.replace("abcd", "zzzz"))


@pytest.mark.parametrize("url", [
    POOLER,
    f"postgresql://postgres.abcd:[{SECRET}]@h.pooler.supabase.com:6543/postgres",
    f"postgresql://postgres.abcd:p@ss{SECRET}@h.pooler.supabase.com:6543/postgres",
    f"postgresql://postgres.abcd:{SECRET}@h.pooler.supabase.com:notaport/postgres",
    f"postgresql://postgres.abcd:{SECRET}@[::1/postgres",
    f"postgresql://:{SECRET}@host/db",
    f"garbage with {SECRET} and an @ sign",
    SECRET,                      # someone pasted just the password into DATABASE_URL
    f"{SECRET}@host/db",         # no scheme
])
def test_no_password_ever_appears_in_any_diagnostic_output(url):
    errors, warnings = url_problems(url, role="pool")
    info = describe_url(url)
    produced = " ".join([mask_url(url), info.masked, info.user, info.host, *errors, *warnings])
    assert SECRET not in produced


def test_is_local_does_not_crash_on_garbage():
    assert is_local("postgresql://u:p@localhost/db") is True
    assert is_local("postgresql://u:[x]@remote.example.com/db") is False


# ── the example file ────────────────────────────────────────────────────────

def test_env_example_cannot_be_used_unedited():
    env = parse_env_text((ROOT / ".env.example").read_text())
    for key in ("DATABASE_URL", "EVENT_BUS_LISTEN_URL"):
        errors, _ = url_problems(env[key], role="pool" if key == "DATABASE_URL" else "listen")
        assert errors, f"{key} in .env.example would pass validation without being edited"
    assert env["PERSISTENCE_BACKEND"] == "postgres" and env["EVENT_BUS_BACKEND"] == "postgres"
    # the destructive-test settings are commented out so a copy cannot drop anything by accident
    assert "TEST_DATABASE_URL" not in env and "ALLOW_DESTRUCTIVE_DB_TESTS" not in env


# ── credentials stay out of git and out of the Modal image ──────────────────

def test_dotenv_is_git_ignored():
    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT)
    if result.returncode == 128:
        pytest.skip("not a git checkout")
    assert result.returncode == 0, ".env must be in .gitignore"


def test_env_files_are_excluded_from_the_modal_image():
    modal = pytest.importorskip("modal")
    import modal_common

    matcher = modal.FilePatternMatcher(*modal_common.IMAGE_IGNORE)
    for secret_file in (".env", ".env.local", ".env.test", "supabase.env", "sub/.env", "sub/.env.prod"):
        assert matcher(Path(secret_file)), f"{secret_file} would be copied into the image"
    for needed in ("src/main.py", "modal_app.py", "src/adapters/persistence/sql/001_create_items.sql", "environment.py"):
        assert not matcher(Path(needed)), f"{needed} must still be shipped"


# ── the guard against dropping a remote database's tables ───────────────────

def _pytest_subprocess(env_extra: dict, target: str):
    env = {k: v for k, v in os.environ.items() if k not in ("TEST_DATABASE_URL", "TEST_LISTEN_DATABASE_URL", "ALLOW_DESTRUCTIVE_DB_TESTS")}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=ROOT, capture_output=True, text=True, env=env, timeout=120,
    )


TRIVIAL = "tests/test_local_supabase_setup.py::test_a_missing_file_is_not_an_error"


def test_pytest_refuses_a_remote_database_without_explicit_consent():
    r = _pytest_subprocess({"TEST_DATABASE_URL": POOLER}, TRIVIAL)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "Refusing to run" in out  # pytest reports a conftest-time exit as a usage error
    assert "ALLOW_DESTRUCTIVE_DB_TESTS=yes" in out and "DROP" in out
    assert SECRET not in out  # the refusal message masks the password


def test_pytest_rejects_an_unusable_test_url_with_a_clear_reason():
    r = _pytest_subprocess(
        {"TEST_DATABASE_URL": "postgresql://postgres.abcd:[YOUR-PASSWORD]@h.pooler.supabase.com:6543/postgres",
         "ALLOW_DESTRUCTIVE_DB_TESTS": "yes"}, TRIVIAL)
    assert r.returncode != 0 and "placeholder" in (r.stdout + r.stderr)


def test_pytest_proceeds_when_the_remote_database_is_explicitly_allowed():
    r = _pytest_subprocess({"TEST_DATABASE_URL": POOLER, "ALLOW_DESTRUCTIVE_DB_TESTS": "yes"}, TRIVIAL)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]


def test_pytest_proceeds_for_a_local_database_without_a_flag():
    r = _pytest_subprocess({"TEST_DATABASE_URL": "postgresql://postgres:pw@localhost:5432/app"}, TRIVIAL)
    assert r.returncode == 0


def test_listen_tests_are_skipped_when_only_the_transaction_pooler_is_configured():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
         "tests/test_postgres_streaming.py::test_listen_notify_wakes_the_pump_immediately"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={**{k: v for k, v in os.environ.items() if not k.startswith("TEST_")},
             "TEST_DATABASE_URL": POOLER, "ALLOW_DESTRUCTIVE_DB_TESTS": "yes"},
    )
    assert "session-level connection" in r.stdout and "1 skipped" in r.stdout


# ── the scripts ─────────────────────────────────────────────────────────────

def _script(name, env_file: Path | None, *extra):
    cmd = [sys.executable, str(ROOT / "scripts" / name), *extra]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    clean = {k: v for k, v in os.environ.items()
             if k not in ("DATABASE_URL", "EVENT_BUS_LISTEN_URL", "EVENT_BUS_BACKEND", "PERSISTENCE_BACKEND")
             and not k.startswith("TEST_")}
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=clean, timeout=90)


@pytest.mark.parametrize("script", ["check_supabase.py", "run_local.py"])
def test_scripts_explain_a_missing_env_file(script, tmp_path):
    r = _script(script, tmp_path / "missing.env")
    assert r.returncode == 2 and "cp .env.example .env" in r.stderr
    assert "Traceback" not in r.stderr


def test_check_supabase_reports_a_bad_url_without_crashing_or_leaking(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"DATABASE_URL=postgresql://postgres.abcd:[{SECRET}]@h.pooler.supabase.com:6543/postgres\n")
    r = _script("check_supabase.py", f)
    assert r.returncode == 1
    assert "wrapped in [ ]" in r.stdout and "Traceback" not in r.stdout + r.stderr
    assert SECRET not in r.stdout + r.stderr


def test_run_local_refuses_to_start_on_a_bad_url_and_does_not_leak(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"PERSISTENCE_BACKEND=postgres\nDATABASE_URL=postgresql://postgres:{SECRET}@h.pooler.supabase.com:6543/postgres\n")
    r = _script("run_local.py", f)
    assert r.returncode == 2 and "postgres.<project-ref>" in r.stderr
    assert "Serving on" not in r.stdout and SECRET not in r.stdout + r.stderr


def test_check_supabase_flags_a_test_url_that_points_at_the_app_database(tmp_path):
    f = tmp_path / ".env"
    same = "postgresql://postgres:pw@localhost:1/app"  # unreachable on purpose: only the identity check matters
    f.write_text(f"DATABASE_URL={same}\nTEST_DATABASE_URL={same}\n")
    r = _script("check_supabase.py", f)
    assert "same database" in r.stdout.lower() or "SAME database" in r.stdout


@requires_postgres
def test_check_supabase_end_to_end_against_a_real_database(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"EVENT_BUS_BACKEND=postgres\nDATABASE_URL={TEST_DATABASE_URL}\n")
    r = _script("check_supabase.py", f)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "connected as" in r.stdout and "DATABASE_URL looks good" in r.stdout
    password = describe_url(TEST_DATABASE_URL)
    raw_password = TEST_DATABASE_URL.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert raw_password not in r.stdout + r.stderr


# ── the password helper ─────────────────────────────────────────────────────

def _set_password(env_file: Path, password: str):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "set_supabase_password.py"), "--env-file", str(env_file), "--stdin"],
        input=password + "\n", cwd=ROOT, capture_output=True, text=True, timeout=60,
    )


def _two_url_env(tmp_path: Path) -> Path:
    f = tmp_path / ".env"
    f.write_text(
        "DATABASE_URL=postgresql://postgres.abcd:[YOUR-PASSWORD]@h.pooler.supabase.com:6543/postgres\n"
        "EVENT_BUS_LISTEN_URL=postgresql://postgres.abcd:[YOUR-PASSWORD]@h.pooler.supabase.com:5432/postgres\n"
        "JWT_SECRET=keep-me\n"
    )
    return f


def test_the_password_is_url_encoded_so_special_characters_survive(tmp_path):
    from urllib.parse import unquote, urlsplit

    nasty = "p@ss:w/rd#?%&= é"
    f = _two_url_env(tmp_path)
    r = _set_password(f, nasty)
    assert r.returncode == 0, r.stderr
    env = parse_env_text(f.read_text())
    for key in ("DATABASE_URL", "EVENT_BUS_LISTEN_URL"):
        parts = urlsplit(env[key])
        assert unquote(parts.password) == nasty      # round-trips exactly
        assert parts.hostname == "h.pooler.supabase.com"
        assert url_problems(env[key], role="pool" if key == "DATABASE_URL" else "listen")[0] == []
    assert env["JWT_SECRET"] == "keep-me"            # nothing else touched
    assert describe_url(env["DATABASE_URL"]).port == 6543 and describe_url(env["EVENT_BUS_LISTEN_URL"]).port == 5432


def test_the_helper_never_prints_the_password_and_keeps_the_file_private(tmp_path):
    f = _two_url_env(tmp_path)
    r = _set_password(f, SECRET)
    assert r.returncode == 0 and "2 line(s)" in r.stdout
    assert SECRET not in r.stdout + r.stderr
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob(".env.*.tmp"))     # no temp file left behind


def test_a_second_run_does_not_clobber_a_password_that_is_already_set(tmp_path):
    f = _two_url_env(tmp_path)
    assert _set_password(f, SECRET).returncode == 0
    before = f.read_text()
    r = _set_password(f, "another-password")
    assert r.returncode == 1 and "already set" in r.stderr
    assert f.read_text() == before


def _run_helper(f: Path, password: str, *flags):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "set_supabase_password.py"), "--env-file", str(f), "--stdin", *flags],
        input=password + "\n", cwd=ROOT, capture_output=True, text=True, timeout=60,
    )


def test_replace_rotates_the_password_in_every_supabase_url_and_touches_nothing_else(tmp_path):
    from urllib.parse import unquote, urlsplit

    f = tmp_path / ".env"
    f.write_text(
        "DATABASE_URL=postgresql://postgres.abcd:OLD-pass@h.pooler.supabase.com:6543/postgres\n"
        "EVENT_BUS_LISTEN_URL=postgresql://postgres.abcd:OLD-pass@h.pooler.supabase.com:5432/postgres\n"
        "# TEST_DATABASE_URL=postgresql://postgres.abcd:OLD-pass@h.pooler.supabase.com:6543/postgres\n"
        "OTHER_DB=postgresql://app:other-db-password@db.example.com:5432/app\n"
        "LOCAL_DB=postgresql://postgres:localpw@localhost:5432/app\n"
        "JWT_SECRET=keep-me\n"
    )
    r = _run_helper(f, "N3w:p@ss/word", "--replace")
    assert r.returncode == 0 and "3 line(s)" in r.stdout, r.stdout + r.stderr
    text = f.read_text()
    assert "OLD-pass" not in text
    env = parse_env_text(text)
    for key in ("DATABASE_URL", "EVENT_BUS_LISTEN_URL"):
        assert unquote(urlsplit(env[key]).password) == "N3w:p@ss/word"
    assert "# TEST_DATABASE_URL=" in text and "OLD-pass" not in text.split("# TEST_DATABASE_URL=")[1].splitlines()[0]
    assert env["OTHER_DB"] == "postgresql://app:other-db-password@db.example.com:5432/app"  # other hosts untouched
    assert env["LOCAL_DB"] == "postgresql://postgres:localpw@localhost:5432/app"
    assert env["JWT_SECRET"] == "keep-me"
    assert "N3w" not in r.stdout + r.stderr


def test_without_replace_an_existing_password_is_left_alone_and_the_hint_names_the_flag(tmp_path):
    f = tmp_path / ".env"
    f.write_text("DATABASE_URL=postgresql://postgres.abcd:OLD@h.pooler.supabase.com:6543/postgres\n")
    r = _run_helper(f, "new")
    assert r.returncode == 1 and "--replace" in r.stderr and "OLD" in f.read_text()


def test_replace_with_no_supabase_url_changes_nothing(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OTHER_DB=postgresql://app:pw@db.example.com:5432/app\n")
    before = f.read_text()
    r = _run_helper(f, "new", "--replace")
    assert r.returncode == 1 and f.read_text() == before


def test_an_empty_password_is_rejected_and_nothing_changes(tmp_path):
    f = _two_url_env(tmp_path)
    before = f.read_text()
    r = _set_password(f, "")
    assert r.returncode == 1 and f.read_text() == before


def test_the_helper_explains_a_missing_env_file(tmp_path):
    r = _set_password(tmp_path / "nope.env", "x")
    assert r.returncode == 2 and "cp .env.example .env" in r.stderr


def test_an_unreplaced_template_is_classified_by_its_host_and_port():
    template = "postgresql://postgres.abcd:[YOUR-PASSWORD]@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres"
    assert describe_url(template).kind == "transaction-pooler"
    assert describe_url(template.replace(":6543", ":5432")).kind == "session-pooler"
    assert describe_url(template).host == "aws-1-ap-northeast-2.pooler.supabase.com"


def test_piped_input_without_the_flag_explains_instead_of_crashing(tmp_path):
    f = _two_url_env(tmp_path)
    before = f.read_text()
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "set_supabase_password.py"), "--env-file", str(f)],
        input="whatever\n", cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 2 and "--stdin" in r.stderr and "Traceback" not in r.stderr
    assert f.read_text() == before
