import modal

from modal_common import build_fastapi_config, get_env_config

# ✅ correct way to get Modal env
MODAL_ENV = modal.config.get("environment") or "dev"

env_config = get_env_config(MODAL_ENV)

APP_NAME = f"{env_config.app_name}-{env_config.env_name}"

app = modal.App(APP_NAME)


@app.function(**build_fastapi_config(env_config))
@modal.asgi_app()
def fastapi_app():
    from src.main import app as fastapi_app
    return fastapi_app