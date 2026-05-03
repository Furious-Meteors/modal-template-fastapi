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
    # Required app config
    env_name: str
    app_name: str = "modal-template-fastapi"

    # Optional custom domain
    custom_domain: Optional[str] = None

    # Hardware config
    cpu_core_count: int = 1
    ram_memory_mib: int = 256
    gpu_type: Optional[str] = None

    # Runtime config
    server_hard_timeout_seconds: int = 150
    min_containers: int = 0

    # Modal resources
    secrets: list = field(default_factory=list)
    volumes: Dict[str, modal.Volume] = field(default_factory=lambda: FASTAPI_VOLUME)


FEAT = EnvConfig(
    env_name="feat",
    custom_domain="feat-app.modal.run",
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
    ],
)

DEV = EnvConfig(
    env_name="dev",
    custom_domain="dev-app.modal.run",
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
    ],
)

PROD = EnvConfig(
    env_name="prod",
    custom_domain="prod-app.modal.run",
    min_containers=1,
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
        "secrets": env.secrets,
        "volumes": env.volumes,
        "min_containers": env.min_containers,
    }

    if env.gpu_type:
        config["gpu"] = env.gpu_type

    return config