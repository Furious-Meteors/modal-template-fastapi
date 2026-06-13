import os
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
    # OpenTelemetry — OTLP/HTTP push to Grafana Cloud (or any OTLP-compatible backend)
    "opentelemetry-sdk>=1.24",
    "opentelemetry-exporter-otlp-proto-http>=1.24",
    "opentelemetry-semantic-conventions>=0.45b0",
    # Injects otelTraceID/otelSpanID into every LogRecord for log-trace correlation
    "opentelemetry-instrumentation-logging>=0.45b0",
]

cpu_image = (
    modal.Image.debian_slim(python_version="3.11")
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

    # OBSERVABILITY — set in prod preset only; None = telemetry disabled (feat/dev)
    otel_endpoint: Optional[str] = None  # Grafana Cloud OTLP base URL
    service_name:  Optional[str] = None  # defaults to app_name when None


FEAT = EnvConfig(
    env_name="feat",
    server_domain="feat-app.modal.run",
    otel_endpoint=None,  # endpoint comes from GRAFANA_OTLP_ENDPOINT inside the grafana-otlp secret
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
        modal.Secret.from_name("grafana-otlp"),
    ],
)

DEV = EnvConfig(
    env_name="dev",
    server_domain="dev-app.modal.run",
    otel_endpoint=None,  # no telemetry in dev — keeps cost at zero
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
        modal.Secret.from_name("grafana-otlp"),
    ],
)

PROD = EnvConfig(
    env_name="prod",
    server_domain="prod-app.modal.run",
    # min_containers=1, # Uncomment to keep 1 warm container in production
    otel_endpoint=None,  # endpoint comes from GRAFANA_OTLP_ENDPOINT inside the grafana-otlp secret
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
        modal.Secret.from_name("grafana-otlp"),
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

def configure_env_vars(env: EnvConfig) -> None:
    _otlp_endpoint = os.environ.get("GRAFANA_OTLP_ENDPOINT") or env.otel_endpoint
    if _otlp_endpoint:
        os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", _otlp_endpoint)
    os.environ.setdefault("OTEL_SERVICE_NAME", env.service_name or env.app_name)
    os.environ.setdefault("MODAL_ENV", env.env_name)


def build_fastapi_config(env: EnvConfig) -> dict:
    config = {
        "image": cpu_image,
        "cpu": env.cpu_core_count,
        "memory": env.ram_memory_mib,
        "timeout": env.server_hard_timeout_seconds,
        "secrets": env.secrets + [modal.Secret.from_dict({"MODAL_ENV": env.env_name})],
        "volumes": env.volumes,
        "min_containers": env.min_containers,
        "enable_memory_snapshot": True,
    }

    if env.gpu_type:
        config["gpu"] = env.gpu_type

    return config