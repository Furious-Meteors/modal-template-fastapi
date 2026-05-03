import os
import modal

from modal_common import build_fastapi_config, get_env_config

MODAL_ENV = os.getenv("MODAL_ENV", "prod")
env_config = get_env_config(MODAL_ENV)

# 🔥 dynamic naming
APP_NAME = f"{env_config.app_name}-{env_config.env_name}"

app = modal.App(APP_NAME)

@app.function(**build_fastapi_config(env_config))
@modal.asgi_app()
def fastapi_app():
    from src.main import app as fastapi_app
    return fastapi_app