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
    monkeypatch.setenv("ALLOW_MEMORY_IN_PROD", "true")  # explicit opt-in: warns instead of refusing
    monkeypatch.delenv("EVENT_BUS_BACKEND", raising=False)
    with caplog.at_level(logging.WARNING, logger="src.deps"):
        deps.init_dependencies()
    assert any("EVENT_BUS_BACKEND=memory" in r.getMessage() for r in caplog.records)


# ── max_containers and the webhook secret ───────────────────────────────────

def test_max_containers_is_validated_and_only_sent_to_modal_when_set():
    import modal_common as mc

    with pytest.raises(ValueError, match="max_containers"):
        EnvConfig(env_name="dev", max_containers=0)
    with pytest.raises(ValueError, match="max_containers"):
        EnvConfig(env_name="dev", min_containers=2, max_containers=1)
    assert "max_containers" not in mc.build_fastapi_config(EnvConfig(env_name="dev"))
    assert mc.build_fastapi_config(EnvConfig(env_name="dev", max_containers=4))["max_containers"] == 4


def test_the_webhook_secret_is_mounted_only_when_named():
    import modal_common as mc

    plain = len(mc.build_fastapi_config(EnvConfig(env_name="dev"))["secrets"])
    with_hooks = len(mc.build_fastapi_config(EnvConfig(env_name="dev", webhook_secret_name="webhook-secrets"))["secrets"])
    assert with_hooks == plain + 1


def test_heartbeat_bounds_how_long_a_dead_stream_holds_its_slot_behind_modal():
    import os

    import modal_common as mc

    assert mc.EnvConfig(env_name="dev").stream_heartbeat_seconds == 5
    for bad in (0, 31):
        with pytest.raises(ValueError, match="stream_heartbeat_seconds"):
            EnvConfig(env_name="dev", stream_heartbeat_seconds=bad)


def test_configure_env_vars_exports_the_heartbeat(monkeypatch):
    import os

    monkeypatch.setenv("STREAM_HEARTBEAT_SECONDS", "x")
    monkeypatch.delenv("STREAM_HEARTBEAT_SECONDS")
    modal_common.configure_env_vars(EnvConfig(env_name="dev", stream_heartbeat_seconds=4))
    assert os.environ["STREAM_HEARTBEAT_SECONDS"] == "4"


# ── prod fails closed; dev and prod are configured like feat ────────────────

def test_prod_refuses_to_start_on_in_memory_storage(monkeypatch):
    from src import deps

    monkeypatch.setenv("MODAL_ENV", "prod")
    monkeypatch.delenv("ALLOW_MEMORY_IN_PROD", raising=False)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "memory")
    monkeypatch.setenv("EVENT_BUS_BACKEND", "memory")
    with pytest.raises(RuntimeError, match="Refusing to start in prod") as exc:
        deps.init_dependencies()
    message = str(exc.value)
    assert "PERSISTENCE_BACKEND=memory" in message and "EVENT_BUS_BACKEND=memory" in message
    assert "ALLOW_MEMORY_IN_PROD=true" in message and "docs/persistence.md" in message


@pytest.mark.parametrize("persistence,bus", [("memory", "postgres"), ("postgres", "memory")])
def test_prod_refuses_if_either_half_is_in_memory(monkeypatch, persistence, bus):
    from src import deps

    monkeypatch.setenv("MODAL_ENV", "prod")
    monkeypatch.delenv("ALLOW_MEMORY_IN_PROD", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to start in prod"):
        deps._refuse_memory_in_prod(persistence, bus)


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_prod_allows_memory_only_with_the_explicit_opt_in(monkeypatch, value):
    from src import deps

    monkeypatch.setenv("MODAL_ENV", "prod")
    monkeypatch.setenv("ALLOW_MEMORY_IN_PROD", value)
    deps._refuse_memory_in_prod("memory", "memory")  # no error


@pytest.mark.parametrize("flag", ["yes", "1", "false", ""])
def test_only_true_counts_as_the_opt_in(monkeypatch, flag):
    from src import deps

    monkeypatch.setenv("MODAL_ENV", "prod")
    monkeypatch.setenv("ALLOW_MEMORY_IN_PROD", flag)
    with pytest.raises(RuntimeError):
        deps._refuse_memory_in_prod("memory", "memory")


@pytest.mark.parametrize("env", ["dev", "feat"])
def test_other_environments_may_use_memory(monkeypatch, env):
    from src import deps

    monkeypatch.setenv("MODAL_ENV", env)
    monkeypatch.delenv("ALLOW_MEMORY_IN_PROD", raising=False)
    deps._refuse_memory_in_prod("memory", "memory")


def test_prod_with_postgres_is_not_refused(monkeypatch):
    from src import deps

    monkeypatch.setenv("MODAL_ENV", "prod")
    monkeypatch.delenv("ALLOW_MEMORY_IN_PROD", raising=False)
    deps._refuse_memory_in_prod("postgres", "postgres")


def test_every_preset_mounts_the_database_and_webhook_secrets_and_caps_containers():
    import modal_common as mc

    for cfg in (mc.FEAT, mc.DEV, mc.PROD):
        assert cfg.persistence_secret_name == "supabase-db", cfg.env_name
        assert cfg.webhook_secret_name == "webhook-secrets", cfg.env_name
        assert cfg.max_containers is not None and cfg.max_containers >= 1, cfg.env_name
        # base secrets + persistence + webhooks + the MODAL_ENV dict
        assert len(mc.build_fastapi_config(cfg)["secrets"]) == len(cfg.secrets) + 3, cfg.env_name


def test_container_caps_keep_database_connections_bounded():
    import modal_common as mc

    # containers x (pool of 5 + 1 LISTEN) pooler clients, per environment
    assert {c.env_name: c.max_containers * 6 for c in (mc.FEAT, mc.DEV, mc.PROD)} == {"feat": 18, "dev": 18, "prod": 30}


# ── metrics export ──────────────────────────────────────────────────────────

def test_metric_export_interval_defaults_to_60s_is_validated_and_exported(monkeypatch):
    import os

    import modal_common as mc

    assert EnvConfig(env_name="dev").metric_export_interval_seconds == 60
    for bad in (4, 601):
        with pytest.raises(ValueError, match="metric_export_interval_seconds"):
            EnvConfig(env_name="dev", metric_export_interval_seconds=bad)
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "x")
    monkeypatch.delenv("OTEL_METRIC_EXPORT_INTERVAL_MS")
    mc.configure_env_vars(EnvConfig(env_name="dev", metric_export_interval_seconds=30))
    assert os.environ["OTEL_METRIC_EXPORT_INTERVAL_MS"] == "30000"


def test_each_container_has_its_own_metric_identity(monkeypatch):
    from src.infrastructure.setup import _build_resource, _instance_id

    monkeypatch.setenv("MODAL_TASK_ID", "ta-ABC123")
    assert _instance_id() == "ta-ABC123"
    assert _build_resource().attributes["service.instance.id"] == "ta-ABC123"
    monkeypatch.delenv("MODAL_TASK_ID")
    assert _instance_id()  # falls back to the hostname, never empty


# ── region pairing ──────────────────────────────────────────────────────────

def test_containers_are_pinned_to_the_database_region_by_default():
    import modal_common as mc

    assert EnvConfig(env_name="dev").region == "us-east"
    for cfg in (mc.FEAT, mc.DEV, mc.PROD):
        assert mc.build_fastapi_config(cfg)["region"] == "us-east", cfg.env_name


def test_region_can_be_changed_or_left_to_modal_but_not_blank():
    import modal_common as mc

    assert mc.build_fastapi_config(EnvConfig(env_name="dev", region="eu-west"))["region"] == "eu-west"
    assert "region" not in mc.build_fastapi_config(EnvConfig(env_name="dev", region=None))
    with pytest.raises(ValueError, match="region"):
        EnvConfig(env_name="dev", region="  ")


# ── database mode and the connection budget ─────────────────────────────────

def test_database_mode_defaults_to_the_safe_pooler_and_is_exported(monkeypatch):
    import os

    import modal_common as mc

    assert EnvConfig(env_name="dev").database_mode == "pooler"
    for key in ("DATABASE_MODE", "DB_POOL_MAX_SIZE"):
        monkeypatch.setenv(key, "x")
        monkeypatch.delenv(key)
    mc.configure_env_vars(EnvConfig(env_name="dev", database_mode="direct", db_pool_size=8))
    assert os.environ["DATABASE_MODE"] == "direct" and os.environ["DB_POOL_MAX_SIZE"] == "8"
    with pytest.raises(ValueError, match="database_mode"):
        EnvConfig(env_name="dev", database_mode="sideways")


def test_a_scale_out_that_would_exhaust_the_database_is_refused_at_config_time():
    # 5 containers x (5 + 1) = 30 connections
    EnvConfig(env_name="dev", max_containers=5, database_connection_limit=30)
    with pytest.raises(ValueError, match="30 connections, over database_connection_limit"):
        EnvConfig(env_name="dev", max_containers=5, database_connection_limit=29)
    with pytest.raises(ValueError, match="db_pool_size"):
        EnvConfig(env_name="dev", db_pool_size=0)
