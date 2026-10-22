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
    # Pinned for reproducible deploys — bump deliberately and re-run the test suite.
    "fastapi==0.143.5",
    "uvicorn[standard]==0.54.0",
    "websockets==17.2",
    "pydantic==2.14.1",
    "PyJWT==2.15.1",
    "asyncpg==0.32.0",  # Postgres/Supabase persistence adapter
    # OpenTelemetry — OTLP/HTTP push to Grafana Cloud (or any OTLP-compatible backend)
    "opentelemetry-sdk==1.45.1",
    "opentelemetry-exporter-otlp-proto-http==1.45.1",
    "opentelemetry-semantic-conventions==0.66b1",
    # Injects otelTraceID/otelSpanID into every LogRecord for log-trace correlation
    "opentelemetry-instrumentation-logging==0.66b1",
]

# Never ship these into the image: a local .env holds real credentials (e.g. the
# Supabase connection string). Secrets reach containers only via Modal Secrets.
IMAGE_IGNORE: List[str] = [
    ".git", ".github", ".venv", "venv", "**/__pycache__", ".pytest_cache",
    "tests", "docs", "scripts",
    ".env", ".env.*", "**/.env", "**/.env.*", "*.env",
]

cpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(*APT_PACKAGES)
    .uv_pip_install(*PIP_PACKAGES)
    .add_local_dir(
        ".",
        remote_path="/root",
        ignore=IMAGE_IGNORE,
    )
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
    ram_memory_mib: int = 512
    gpu_type: Optional[str] = None

    # RUNTIME CONFIGURATION
    server_hard_timeout_seconds: int = 150
    min_containers: int = 0
    # Upper bound on containers. Every container holds up to DB_POOL_MAX_SIZE (+1 LISTEN)
    # connections to the database pooler, so containers x (pool + 1) must stay under your
    # Supabase plan's pooler client limit. None = Modal's default (unbounded scale-out).
    max_containers: Optional[int] = None
    max_concurrent_requests: int = 5

    # STREAMING — each open SSE/WebSocket connection holds one concurrent-request
    # slot for its whole life. Cap them below max_concurrent_requests so streams
    # can never starve normal requests (validated in __post_init__).
    max_streams: int = 3
    # Streams end themselves this many seconds before the hard timeout so
    # clients reconnect cleanly instead of being cut off.
    stream_timeout_margin_seconds: int = 30
    # Behind Modal's proxy the app is NOT told when a client disconnects: it only finds out
    # when the next heartbeat write fails. Until then the dead stream keeps its slot, and a
    # reconnecting browser gets 503 (measured live: slots stayed taken ~10s with a 15s
    # heartbeat). So this is also the worst-case time a closed stream holds its slot.
    stream_heartbeat_seconds: int = 5

    # PERSISTENCE — name of a Modal secret holding PERSISTENCE_BACKEND=postgres and
    # DATABASE_URL (the Supabase pooler URL). None keeps the in-memory backend.
    # The secret must exist before you deploy; see docs/persistence.md.
    persistence_secret_name: Optional[str] = None

    # WEBHOOKS — name of a Modal secret holding WEBHOOK_SECRET_<PROVIDER> variables.
    # None = no provider configured, so POST /webhooks/<provider> answers 404.
    webhook_secret_name: Optional[str] = None

    # MODAL RESOURCES
    secrets: list = field(default_factory=list)
    volumes: Dict[str, modal.Volume] = field(default_factory=lambda: FASTAPI_VOLUME)

    # CORS — restrict origins per environment; ["*"] allows all (dev only)
    cors_origins: List[str] = field(default_factory=lambda: ["*"])

    # OBSERVABILITY — set in prod preset only; None = telemetry disabled (feat/dev)
    telemetry_enabled: bool = True       # False = no OTLP export and no grafana-otlp secret mounted
    otel_endpoint: Optional[str] = None  # Grafana Cloud OTLP base URL
    service_name:  Optional[str] = None  # defaults to app_name when None
    # How often each container pushes metrics. Every container pushes on its own, so the
    # request rate to the backend is containers / interval. 60s keeps that low. NOTE this does
    # not make Grafana Cloud's OTLP gateway stop answering the occasional 429: it did so at
    # 60s too (about 1 export in 5 over a 17-minute soak with 3 containers, and from a single
    # probe with no app running), so the SDK's retry is what absorbs them. Counters and
    # histograms are cumulative, so a rejected batch costs one data point, never data.
    metric_export_interval_seconds: int = 60

    def __post_init__(self) -> None:
        if self.max_containers is not None and self.max_containers < 1:
            raise ValueError(f"max_containers must be >= 1 or None (got {self.max_containers})")
        if self.max_containers is not None and self.max_containers < self.min_containers:
            raise ValueError("max_containers must be >= min_containers")
        if not 1 <= self.max_streams < self.max_concurrent_requests:
            raise ValueError(
                f"max_streams ({self.max_streams}) must be >= 1 and less than "
                f"max_concurrent_requests ({self.max_concurrent_requests}), "
                f"otherwise open streams can starve regular requests"
            )
        if not 5 <= self.metric_export_interval_seconds <= 600:
            raise ValueError(f"metric_export_interval_seconds must be 5-600 (got {self.metric_export_interval_seconds})")
        if not 1 <= self.stream_heartbeat_seconds <= 30:
            raise ValueError(f"stream_heartbeat_seconds must be 1-30 (got {self.stream_heartbeat_seconds})")
        if self.server_hard_timeout_seconds - self.stream_timeout_margin_seconds < 10:
            raise ValueError(
                "server_hard_timeout_seconds must exceed stream_timeout_margin_seconds by at least 10"
            )


FEAT = EnvConfig(
    env_name="feat",
    server_domain="feat-app.modal.run",
    # Supabase settings (PERSISTENCE_BACKEND, EVENT_BUS_BACKEND, DATABASE_URL, ...).
    # The secret must exist in the Modal environment "feat" before `modal deploy`:
    #   python scripts/push_modal_secret.py --environment feat
    persistence_secret_name="supabase-db",
    # Provider secrets for webhooks: python scripts/push_modal_secret.py --environment feat \
    #     --name webhook-secrets --keys WEBHOOK_SECRET_ACME
    webhook_secret_name="webhook-secrets",
    # Bounds database connections: 3 containers x (pool of 5 + 1 LISTEN) = 18 pooler clients.
    # (Set to 1 temporarily to verify the per-container stream cap and rate limits live.)
    max_containers=3,
    cors_origins=["*"],
    otel_endpoint=None,  # endpoint comes from GRAFANA_OTLP_ENDPOINT inside the grafana-otlp secret
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
        modal.Secret.from_name("grafana-otlp"),
    ],
)

DEV = EnvConfig(
    env_name="dev",
    server_domain="dev-app.modal.run",
    cors_origins=["*"],  # TODO: replace with your actual Modal domain when known
    telemetry_enabled=False,  # dev is silent — keeps cost at zero
    otel_endpoint=None,
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
    ],
    # Same persistence and webhook secrets as every environment. Create them in the Modal
    # environment "dev" BEFORE deploying (modal deploy fails if a secret is missing):
    #   python scripts/push_modal_secret.py --environment dev
    #   python scripts/push_modal_secret.py --environment dev --name webhook-secrets --keys WEBHOOK_SECRET_<PROVIDER>
    persistence_secret_name="supabase-db",
    webhook_secret_name="webhook-secrets",
    max_containers=3,  # bounds database connections: 3 x (pool of 5 + 1 LISTEN)
)

PROD = EnvConfig(
    env_name="prod",
    server_domain="prod-app.modal.run",
    cors_origins=["*"],  # TODO: replace with your actual Modal domain when known
    # min_containers=1, # Uncomment to keep 1 warm container in production
    otel_endpoint=None,  # endpoint comes from GRAFANA_OTLP_ENDPOINT inside the grafana-otlp secret
    secrets=[
        modal.Secret.from_name("fastapi-auth-secrets"),
        modal.Secret.from_name("grafana-otlp"),
    ],
    # Create these in the Modal environment "prod" BEFORE deploying; prod REFUSES to start on
    # in-memory storage (see deps.init_dependencies), so a missing database config is loud.
    #   python scripts/push_modal_secret.py --environment prod
    #   python scripts/push_modal_secret.py --environment prod --name webhook-secrets --keys WEBHOOK_SECRET_<PROVIDER>
    persistence_secret_name="supabase-db",
    webhook_secret_name="webhook-secrets",
    # Bounds database connections: containers x (pool of 5 + 1 LISTEN) must stay under your
    # Supabase plan's pooler client limit. 5 x 6 = 30 — check your plan, then raise deliberately.
    max_containers=5,
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
    # Read by src/infrastructure/setup.py — "false" forces the no-op providers
    # even if an OTLP endpoint happens to be present in the environment.
    os.environ["TELEMETRY_ENABLED"] = "true" if env.telemetry_enabled else "false"
    if env.telemetry_enabled:
        _otlp_endpoint = os.environ.get("GRAFANA_OTLP_ENDPOINT") or env.otel_endpoint
        if _otlp_endpoint:
            os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", _otlp_endpoint)
    os.environ.setdefault("OTEL_SERVICE_NAME", env.service_name or env.app_name)
    os.environ.setdefault("MODAL_ENV", env.env_name)
    # App metadata — read by src/utils/config.py so src/ never imports modal_common directly
    os.environ.setdefault("APP_NAME", env.app_name)
    os.environ.setdefault("APP_VERSION", env.app_version)
    os.environ.setdefault("APP_DESCRIPTION", env.app_description)
    os.environ.setdefault("SERVER_PREFIX", env.server_prefix)
    os.environ.setdefault("CORS_ORIGINS", ",".join(env.cors_origins))
    # Streaming limits — read by src/utils/config.py
    os.environ.setdefault("STREAM_MAX_CONNECTIONS", str(env.max_streams))
    os.environ.setdefault("STREAM_HEARTBEAT_SECONDS", str(env.stream_heartbeat_seconds))
    os.environ.setdefault("OTEL_METRIC_EXPORT_INTERVAL_MS", str(env.metric_export_interval_seconds * 1000))
    os.environ.setdefault(
        "STREAM_MAX_DURATION_SECONDS",
        str(env.server_hard_timeout_seconds - env.stream_timeout_margin_seconds),
    )


def build_fastapi_config(env: EnvConfig) -> dict:
    config = {
        "image": cpu_image,
        "cpu": env.cpu_core_count,
        "memory": env.ram_memory_mib,
        "timeout": env.server_hard_timeout_seconds,
        "secrets": env.secrets
        + ([modal.Secret.from_name(env.persistence_secret_name)] if env.persistence_secret_name else [])
        + ([modal.Secret.from_name(env.webhook_secret_name)] if env.webhook_secret_name else [])
        + [modal.Secret.from_dict({"MODAL_ENV": env.env_name})],
        "volumes": env.volumes,
        "min_containers": env.min_containers,
        "enable_memory_snapshot": True,
    }

    if env.max_containers is not None:
        config["max_containers"] = env.max_containers
    if env.gpu_type:
        config["gpu"] = env.gpu_type

    return config