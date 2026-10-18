"""
Tests for per-environment telemetry gating in modal_common.

Pure config logic — no Modal API calls: modal.Secret.from_name is lazy and
nothing here deploys or connects to Modal.
"""
import os
from unittest import mock

import pytest

from modal_common import DEV, FEAT, PROD, configure_env_vars


def test_dev_has_telemetry_disabled():
    assert DEV.telemetry_enabled is False


@pytest.mark.parametrize("env", [FEAT, PROD])
def test_feat_and_prod_have_telemetry_enabled(env):
    assert env.telemetry_enabled is True


def test_dev_does_not_mount_grafana_secret():
    assert len(DEV.secrets) == 1
    assert len(FEAT.secrets) == 2
    assert len(PROD.secrets) == 2


def test_configure_env_vars_dev_sets_telemetry_false_and_skips_endpoint():
    with mock.patch.dict(os.environ, {"GRAFANA_OTLP_ENDPOINT": "https://otlp.example"}, clear=False):
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        configure_env_vars(DEV)
        assert os.environ["TELEMETRY_ENABLED"] == "false"
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ


def test_configure_env_vars_prod_sets_telemetry_true_and_endpoint():
    with mock.patch.dict(os.environ, {"GRAFANA_OTLP_ENDPOINT": "https://otlp.example"}, clear=False):
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        configure_env_vars(PROD)
        assert os.environ["TELEMETRY_ENABLED"] == "true"
        assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otlp.example"
