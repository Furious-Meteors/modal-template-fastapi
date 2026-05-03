import modal

from modal_common import build_fastapi_config, get_env_config

# SETTING MODAL ENVIRONMENT
MODAL_ENV = modal.config.get("environment") or "dev"
env_config = get_env_config(MODAL_ENV)

# SETTING MODAL APP
APP_NAME = f"{env_config.app_name}-{env_config.env_name}"
app = modal.App(APP_NAME)

# SETTING MODAL PROJECT
@app.function(**build_fastapi_config(env_config))
@modal.asgi_app()
def fastapi_app():
    from src.main import app as fastapi_app
    return fastapi_app