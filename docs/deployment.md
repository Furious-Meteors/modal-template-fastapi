# Deployment

How the application is packaged, configured, and deployed to Modal's serverless infrastructure — including principles, environment matrix, and operational runbook.

---

## Deployment Principles

<div class="grid cards" markdown>

-   :material-server-off:{ .lg .middle } **Serverless-first**

    ---

    No EC2, no VMs, no Kubernetes. Modal provisions containers on demand and scales to zero when idle. The `feat` and `dev` environments pay nothing when unused.

-   :material-shield-lock:{ .lg .middle } **Secret isolation**

    ---

    Secrets (`JWT_SECRET`, etc.) are injected by Modal at runtime from named secret stores — never baked into the image, never in version control.

-   :material-scale-balance:{ .lg .middle } **Environment parity**

    ---

    All environments run identical container images. The only differences are hardware sizing, min-container count, and the secrets injected. No environment-specific code paths.

-   :material-layers:{ .lg .middle } **Declarative config**

    ---

    `EnvConfig` is a Python dataclass — environments are defined in code, reviewed in PRs, and versioned with the application. No YAML config sprawl.

-   :material-gate:{ .lg .middle } **Test-gated deploys**

    ---

    No code reaches Modal without passing all 79 tests. The `needs: test` gate in GitHub Actions is the single enforcement point.

-   :material-database:{ .lg .middle } **Persistent volumes**

    ---

    A Modal Volume is mounted at `/root/fastapi-volume` for durable storage across container restarts and deployments.

</div>

---

## Environment Matrix

| Config | `feat` | `dev` | `prod` |
|---|---|---|---|
| Modal app name | `modal-template-fastapi-feat` | `modal-template-fastapi-dev` | `modal-template-fastapi-prod` |
| Custom domain | `feat-app.modal.run` | `dev-app.modal.run` | `prod-app.modal.run` |
| CPU cores | 1 | 1 | 1 |
| RAM (MiB) | 256 | 256 | 256 |
| Min containers | 0 | 0 | **1** |
| GPU | None | None | None |
| Timeout (s) | 150 | 150 | 150 |
| Deploy trigger | `push feat/**` | PR merged → `dev` | PR merged → `production` |
| Modal secrets | `fastapi-auth-secrets` | `fastapi-auth-secrets` | `fastapi-auth-secrets` |

!!! info "Min containers"
    `min_containers=1` in `prod` keeps one container always warm, eliminating cold start latency for real users. `feat` and `dev` scale to zero and incur a cold start (~1-3s) on the first request.

---

## `EnvConfig` Reference

Defined in `modal_common.py`. Every field maps directly to a Modal function kwarg via `build_fastapi_config()`.

| Field | Type | Default | Description |
|---|---|---|---|
| `env_name` | `str` | — | **Required.** Short identifier: `feat`, `dev`, `prod` |
| `app_name` | `str` | `"modal-template-fastapi"` | Base Modal app name. Final name is `{app_name}-{env_name}` |
| `custom_domain` | `str \| None` | `None` | Modal subdomain for this environment |
| `cpu_core_count` | `int` | `1` | vCPU count per container |
| `ram_memory_mib` | `int` | `256` | Memory limit in MiB per container |
| `gpu_type` | `str \| None` | `None` | GPU SKU string (e.g. `"A10G"`, `"H100"`) — `None` for CPU-only |
| `server_hard_timeout_seconds` | `int` | `150` | Maximum request duration before hard kill |
| `min_containers` | `int` | `0` | Minimum always-running containers |
| `secrets` | `list` | `[]` | List of `modal.Secret` objects injected at runtime |
| `volumes` | `Dict[str, Volume]` | `FASTAPI_VOLUME` | Volume mounts as `{path: volume}` |

### Adding GPU support

```python
STAGING = EnvConfig(
    env_name="staging",
    gpu_type="A10G",          # or "T4", "H100", "L4", etc.
    ram_memory_mib=4096,      # bump RAM for GPU workloads
    secrets=[modal.Secret.from_name("fastapi-auth-secrets")],
)
```

---

## Container Image

The image is defined once in `modal_common.py` and shared across all environments:

```python
cpu_image = (
    modal.Image.debian_slim(python_version="3.10")  # (1)
    .apt_install("curl", "jq")                       # (2)
    .uv_pip_install(                                 # (3)
        "fastapi",
        "uvicorn[standard]",
        "websockets",
        "pydantic",
        "PyJWT",
    )
    .add_local_dir(".", remote_path="/root")         # (4)
)
```

1. Slim Debian base — minimal attack surface
2. System packages: `curl` for health probes, `jq` for JSON manipulation in scripts
3. `uv_pip_install` uses the fast `uv` resolver — significantly faster than vanilla pip
4. Copies the entire repo into `/root` inside the container — your application code is available at `/root/src/`

!!! tip "Customising the image"
    Add Python packages by appending to the `PIP_PACKAGES` list. Add OS-level tools to `APT_PACKAGES`. Modal rebuilds and caches the image layer automatically on the next deploy.

---

## Persistent Volume

```python
volume = modal.Volume.from_name("fastapi-volume", create_if_missing=True)

FASTAPI_VOLUME = {
    "/root/fastapi-volume": volume   # mount path → Volume object
}
```

The volume is created once and reused across all container instances and all deployments. Data written to `/root/fastapi-volume/` inside the container persists across restarts.

---

## Running Locally

No cloud deployment needed for development:

```bash
# Uses the local_entrypoint — starts uvicorn on port 8000
MODAL_ENV=dev modal run modal_app.py

# Test the running server
curl http://localhost:8000/api/v1/health
```

The `@app.local_entrypoint()` in `modal_app.py` imports the FastAPI app and starts `uvicorn` directly — the same application code runs locally and in production.

---

## Manual Deployment

=== "feat"

    ```bash
    MODAL_ENV=feat modal deploy modal_app.py
    ```

=== "dev"

    ```bash
    MODAL_ENV=dev modal deploy modal_app.py
    ```

=== "prod"

    ```bash
    MODAL_ENV=prod modal deploy modal_app.py
    ```

!!! warning "Prefer CI/CD for production"
    Manual deploys bypass the test gate. Use `modal deploy` directly only for emergency hotfixes or local iteration on `feat`.

---

## Operational Runbook

### First-time setup

```bash
# 1. Install Modal
pip install modal

# 2. Authenticate
modal setup   # opens browser for OAuth

# 3. Create the named secret store in Modal
modal secret create fastapi-auth-secrets \
  JWT_SECRET="$(python -c "import secrets; print(secrets.token_hex(32))")"

# 4. Deploy to feat
MODAL_ENV=feat modal deploy modal_app.py
```

### Viewing logs

```bash
# Stream logs from the feat environment
modal app logs modal-template-fastapi-feat

# Or open the Modal dashboard
modal app list
```

### Checking deployment status

```bash
modal app list
# NAME                              STATE    CREATED
# modal-template-fastapi-feat       deployed 2026-01-01 12:00:00
# modal-template-fastapi-dev        deployed 2026-01-01 11:00:00
# modal-template-fastapi-prod       deployed 2026-01-01 10:00:00
```

### Stopping an environment

```bash
modal app stop modal-template-fastapi-feat
```

---

## Adding a New Environment

1. **Define the config** in `modal_common.py`:

    ```python
    STAGING = EnvConfig(
        env_name="staging",
        custom_domain="staging-app.modal.run",
        min_containers=0,
        secrets=[modal.Secret.from_name("fastapi-auth-secrets")],
    )

    ENV_CONFIGS = {
        "feat": FEAT,
        "dev": DEV,
        "staging": STAGING,   # ← add here
        "prod": PROD,
    }
    ```

2. **Add a GitHub Variable** `STAGING = staging` in repo Settings → Variables

3. **Add routing** in `modal-deploy.yml` `Set Modal environment` step:

    ```bash
    elif [ "${{ github.base_ref }}" = "staging" ]; then
        echo "deploy_env=${{ vars.STAGING }}" >> "$GITHUB_OUTPUT"
    fi
    ```

4. **Add the trigger** to `modal-deploy.yml` `on.pull_request.branches`:

    ```yaml
    branches:
      - dev
      - staging      # ← add here
      - production
    ```
