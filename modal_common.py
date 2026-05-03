from dataclasses import dataclass, field
from typing import Dict, List, Optional

import modal

# ---------------------------------------------------------------------------
# FASTAPI TEMPLATE MODAL IMAGE
# ---------------------------------------------------------------------------

APT_PACKAGES: List[str] = [
    "curl",
    "jq",
]

PIP_PACKAGES: List[str] = [
    "fastapi",
    "uvicorn[standard]",
    "websockets",
    "pydantic",
    "PyJWT",
]

cpu_image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install(*APT_PACKAGES)
    .uv_pip_install(*PIP_PACKAGES)
    .add_local_dir(".", remote_path="/root")
)

# ---------------------------------------------------------------------------
# FASTAPI MODAL VOLUME
# ---------------------------------------------------------------------------

volume = modal.Volume.from_name("fastapi-volume", create_if_missing=True)

FASTAPI_VOLUME = {
    "/root/fastapi-volume": volume
}

# ---------------------------------------------------------------------------
# ENVIRONMENT CONFIGURATION
# ---------------------------------------------------------------------------

@dataclass
class EnvConfig:
    # APP CONFIGURATION
    env_name: str
    app_name: str = "modal-template-fastapi"
    app_version: str = "1.0.0"
    app_description: str = "A FastAPI template deployed on Modal with CRUD endpoints."

    # SERVER CONFIGURATION
    server_port: int = 8000
    server_host: str = "0.0.0.0"
    server_reload: bool = False
    server_prefix: str = "/api/v1"
 
    # CUSTOM SERVER DOMAIN(OPTIONAL)
    server_domain: Optional[str] = None

    # HARDWARE CONFIGURATION
    cpu_core_count: int = 1
    ram_memory_mib: int = 256
    gpu_type: Optional[str] = None

    # RUNTIME CONFIGURATION
    server_hard_timeout_seconds: int = 150
    min_containers: int = 0
    max_concurrent_requests: int = 5

    # MODAL RESOURCES
    secrets: list = field(default_factory=list)
    volumes: Dict[str, modal.Volume] = field(default_factory=lambda: FASTAPI_VOLUME)


FEAT = EnvConfig(
    env_name="feat",
    server_domain="feat-app.modal.run",
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
    ],
)

DEV = EnvConfig(
    env_name="dev",
    server_domain="dev-app.modal.run",
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
    ],
)

PROD = EnvConfig(
    env_name="prod",
    server_domain="prod-app.modal.run",
    # min_containers=1, # Uncomment this to run 1 container in production, when building Apps
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
    ],
)

ENV_CONFIGS = {
    "feat": FEAT,
    "dev": DEV,
    "prod": PROD,
}

def get_env_config(env_name: str) -> EnvConfig:
    env_name = env_name.lower().strip()

    if env_name not in ENV_CONFIGS:
        raise ValueError(
            f"Invalid MODAL_ENV='{env_name}'. "
            f"Expected one of: {', '.join(ENV_CONFIGS.keys())}"
        )

    return ENV_CONFIGS[env_name]

def build_fastapi_config(env: EnvConfig) -> dict:
    config = {
        "image": cpu_image,
        "cpu": env.cpu_core_count,
        "memory": env.ram_memory_mib,
        "timeout": env.server_hard_timeout_seconds,
        "secrets": env.secrets + [modal.Secret.from_dict({"MODAL_ENV": env.env_name})],
        "volumes": env.volumes,
        "min_containers": env.min_containers,
    }

    if env.gpu_type:
        config["gpu"] = env.gpu_type

    return config