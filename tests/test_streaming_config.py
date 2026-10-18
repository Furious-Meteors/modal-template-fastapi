"""Config and capacity rules for streaming."""
import pytest

import modal_common
from modal_common import EnvConfig


def test_default_stream_cap_is_below_concurrency():
    cfg = EnvConfig(env_name="dev")
    assert 1 <= cfg.max_streams < cfg.max_concurrent_requests


@pytest.mark.parametrize("streams", [0, 5, 9])
def test_stream_cap_must_leave_room_for_regular_requests(streams):
    with pytest.raises(ValueError, match="max_streams"):
        EnvConfig(env_name="dev", max_streams=streams, max_concurrent_requests=5)


def test_raising_concurrency_allows_more_streams():
    assert EnvConfig(env_name="dev", max_streams=8, max_concurrent_requests=10).max_streams == 8


def test_timeout_margin_must_leave_a_usable_stream_lifetime():
    with pytest.raises(ValueError, match="server_hard_timeout_seconds"):
        EnvConfig(env_name="dev", server_hard_timeout_seconds=35, stream_timeout_margin_seconds=30)


def test_all_presets_are_valid():
    for cfg in modal_common.ENV_CONFIGS.values():
        assert cfg.max_streams < cfg.max_concurrent_requests


def test_configure_env_vars_exports_stream_limits(monkeypatch):
    for key in ("STREAM_MAX_CONNECTIONS", "STREAM_MAX_DURATION_SECONDS"):
        monkeypatch.setenv(key, "x")  # registers cleanup for the real assignment below
        monkeypatch.delenv(key)
    cfg = EnvConfig(env_name="dev", server_hard_timeout_seconds=150)
    modal_common.configure_env_vars(cfg)
    import os

    assert os.environ["STREAM_MAX_CONNECTIONS"] == "3"
    # Streams end 30s before the hard timeout so clients reconnect cleanly.
    assert os.environ["STREAM_MAX_DURATION_SECONDS"] == "120"


@pytest.mark.parametrize("raw", ["abc", "0", "-3"])
def test_bad_stream_env_fails_fast(monkeypatch, raw):
    from src.utils.config import _positive_int

    monkeypatch.setenv("STREAM_MAX_CONNECTIONS", raw)
    with pytest.raises(RuntimeError, match="STREAM_MAX_CONNECTIONS"):
        _positive_int("STREAM_MAX_CONNECTIONS", 50)


def test_stream_env_defaults(monkeypatch):
    from src.utils.config import _positive_int

    monkeypatch.delenv("STREAM_MAX_CONNECTIONS", raising=False)
    assert _positive_int("STREAM_MAX_CONNECTIONS", 50) == 50
    monkeypatch.setenv("STREAM_MAX_CONNECTIONS", "  ")
    assert _positive_int("STREAM_MAX_CONNECTIONS", 50) == 50


def test_unknown_event_bus_backend_fails_at_startup(monkeypatch):
    from src import deps

    monkeypatch.setenv("EVENT_BUS_BACKEND", "carrier-pigeon")
    deps._get_event_bus.cache_clear()
    try:
        with pytest.raises(ValueError, match="EVENT_BUS_BACKEND"):
            deps._get_event_bus()
    finally:
        deps._get_event_bus.cache_clear()


def test_memory_event_bus_warns_in_prod(monkeypatch, caplog):
    import logging

    from src import deps

    monkeypatch.setenv("MODAL_ENV", "prod")
    monkeypatch.delenv("EVENT_BUS_BACKEND", raising=False)
    with caplog.at_level(logging.WARNING, logger="src.deps"):
        deps.init_dependencies()
    assert any("EVENT_BUS_BACKEND=memory" in r.getMessage() for r in caplog.records)
