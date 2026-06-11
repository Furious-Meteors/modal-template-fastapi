import os

import modal

from modal_common import build_fastapi_config, get_env_config

# SETTING MODAL ENVIRONMENT
MODAL_ENV = os.environ.get("MODAL_ENV", "dev")

# SETTING MODAL ENVIRONMENT CONFIG
env_config = get_env_config(MODAL_ENV)

# SETTING MODAL APP
APP_NAME = f"{env_config.app_name}-{env_config.env_name}"
app = modal.App(APP_NAME)

# Inject OTel env vars before the Cls is instantiated so that @enter reads the
# correct values at container startup. setup.py reads these at call time (never
# at import time), so setting them here — before the first request — is safe.
#
# Priority order for the OTLP endpoint:
#   1. GRAFANA_OTLP_ENDPOINT — injected by the grafana-otlp Modal secret (preferred)
#   2. env_config.otel_endpoint — code-level value, useful as a local dev override
# setdefault means whatever is already in the environment (from the secret) always wins.
_otlp_endpoint = os.environ.get("GRAFANA_OTLP_ENDPOINT") or env_config.otel_endpoint
if _otlp_endpoint:
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", _otlp_endpoint)
os.environ.setdefault("OTEL_SERVICE_NAME", env_config.service_name or env_config.app_name)
os.environ.setdefault("MODAL_ENV", env_config.env_name)


# SETTING MODAL PROJECT
@app.cls(**build_fastapi_config(env_config))
@modal.concurrent(max_inputs=env_config.max_concurrent_requests)
class FastAPIService:
    @modal.enter()
    def startup(self) -> None:
        # Runs once per container during warmup — never on the request hot path.
        # SDK init cost (~15ms) is paid here so live requests stay at ~2-5µs overhead.
        from src.observability import setup_telemetry
        setup_telemetry()

    @modal.asgi_app()
    def fastapi_app(self):
        from src.main import app as fastapi_app
        return fastapi_app


@app.local_entrypoint()
def main():
    # Mirror what @enter does in the Modal container so telemetry works locally too.
    # The env vars above are already set; setup_telemetry() reads them at call time.
    from src.observability import setup_telemetry
    setup_telemetry()
    from src.main import app as fastapi_app
    from uvicorn import run
    run(fastapi_app, host=env_config.server_host, port=env_config.server_port)
