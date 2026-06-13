import os

import modal

from modal_common import build_fastapi_config, configure_env_vars, get_env_config

# SETTING MODAL ENVIRONMENT
MODAL_ENV = os.environ.get("MODAL_ENV", "dev")

# SETTING MODAL ENVIRONMENT CONFIG
env_config = get_env_config(MODAL_ENV)

# SETTING MODAL APP
APP_NAME = f"{env_config.app_name}-{env_config.env_name}"
app = modal.App(APP_NAME)

configure_env_vars(env_config)


# SETTING MODAL PROJECT
@app.cls(**build_fastapi_config(env_config))
@modal.concurrent(max_inputs=env_config.max_concurrent_requests)
class FastAPIService:
    @modal.enter(snap=True)
    def preload(self) -> None:
        # Runs once before the CPU snapshot is taken (modal deploy only).
        # Pre-importing the FastAPI app and all its dependencies bakes them into
        # the snapshot so subsequent cold starts restore from memory (~50-150ms)
        # instead of re-importing every module from disk (~300-800ms).
        import src.main  # noqa: F401

    @modal.enter(snap=False)
    def startup(self) -> None:
        # Runs once per container after snapshot restore — never on the request hot path.
        # Network-bound setup (OTLP connections) must live here; they cannot survive
        # a snapshot because file descriptors and sockets are not portable across restores.
        from src.infrastructure import record_cold_start, setup_telemetry
        setup_telemetry()
        record_cold_start()  # fires against the real MeterProvider — always exported

    @modal.asgi_app()
    def fastapi_app(self):
        # src.main is already in sys.modules from preload() — this is a cache hit.
        from src.main import app as fastapi_app
        return fastapi_app


@app.local_entrypoint()
def main():
    # Mirror what @enter does in the Modal container so telemetry works locally too.
    # The env vars above are already set; setup_telemetry() reads them at call time.
    from src.infrastructure import setup_telemetry
    setup_telemetry()
    from src.main import app as fastapi_app
    from uvicorn import run
    run(fastapi_app, host=env_config.server_host, port=env_config.server_port)
