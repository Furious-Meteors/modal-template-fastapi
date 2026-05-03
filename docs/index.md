---
title: Home
hide:
  - toc
---

<div align="center" markdown>

<p class="hero-title">modal-template-fastapi</p>

<p class="hero-subtitle">
  A production-ready FastAPI template deployed serverlessly on Modal —<br>
  JWT authentication, full CRUD API, 79-test pytest suite, and automated CI/CD.
</p>

<div class="hero-badges">

![Python](https://img.shields.io/badge/Python-3.11-4f46e5?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-009688?style=flat-square&logo=fastapi&logoColor=white)
![Modal](https://img.shields.io/badge/Modal-Serverless-7c3aed?style=flat-square)
![Tests](https://img.shields.io/badge/Tests-79%20passing-15803d?style=flat-square&logo=pytest&logoColor=white)
![Node24](https://img.shields.io/badge/Actions-Node%2024-4f46e5?style=flat-square&logo=githubactions&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-6b7280?style=flat-square)

</div>

</div>

---

## What is this?

`modal-template-fastapi` is a **battle-tested starting point** for shipping Python APIs to production. It combines the ergonomics of FastAPI with the power of Modal's serverless platform — giving you autoscaling, secret management, persistent volumes, and environment-aware deployments without managing any infrastructure.

Clone it, swap in your business logic, and ship in minutes.

---

## Features

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Serverless on Modal**

    ---

    Runs on Modal's container infrastructure with zero server management. Configurable CPU, RAM, GPU, and minimum-container settings per environment via the `EnvConfig` dataclass.

    [:octicons-arrow-right-24: Deployment guide](deployment.md)

-   :material-lock-outline:{ .lg .middle } **JWT Authentication**

    ---

    Every protected endpoint validates Bearer tokens against a configurable secret and algorithm. Clear, machine-readable error codes — `MISSING_TOKEN`, `TOKEN_EXPIRED`, `INVALID_TOKEN` — on every failure path.

    [:octicons-arrow-right-24: Auth guide](api/auth.md)

-   :material-database-outline:{ .lg .middle } **Full CRUD API**

    ---

    Five REST endpoints across health and items, built on Pydantic v2 models with automatic request validation, typed responses, and standardised `session_id` tracing on every reply.

    [:octicons-arrow-right-24: API reference](api/endpoints.md)

-   :material-test-tube:{ .lg .middle } **79-Test Pytest Suite**

    ---

    Four test modules covering auth, CRUD routes, and all Pydantic models. Isolated in-memory store reset per test, dependency override pattern for focused unit tests, and zero environment secrets required.

    [:octicons-arrow-right-24: Testing guide](testing.md)

-   :material-source-branch:{ .lg .middle } **Environment-Aware CI/CD**

    ---

    GitHub Actions routes deployments to `feat`, `dev`, or `prod` Modal environments based on branch and PR target. Tests must pass before any deploy runs — enforced via `needs: test`.

    [:octicons-arrow-right-24: CI/CD reference](cicd.md)

-   :material-layers-outline:{ .lg .middle } **Multi-Environment Model**

    ---

    Declarative `EnvConfig` dataclass defines hardware, secrets, domains, and scaling per environment. Adding a new environment is a four-line change, no YAML sprawl.

    [:octicons-arrow-right-24: Architecture](architecture.md)

</div>

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/your-org/modal-template-fastapi.git
cd modal-template-fastapi

# Install test dependencies
pip install -r .github/requirements/test.txt

# Authenticate with Modal (first time only)
modal setup
```

### 2. Run tests

```bash
pytest tests/ -v
# ======================== 79 passed in 0.11s ========================
```

### 3. Run locally

```bash
# Serve via Modal's local entrypoint — no cloud required
MODAL_ENV=dev modal run modal_app.py

# API available at http://localhost:8000
curl http://localhost:8000/api/v1/health
```

### 4. Deploy

```bash
# Deploy to your feat environment manually
MODAL_ENV=feat modal deploy modal_app.py

# Or push a branch and let CI/CD handle it
git push origin feat/my-feature
```

---

## Project Structure

```
modal-template-fastapi/
│
├── src/                         # Application source
│   ├── main.py                  # FastAPI factory, CORS, exception handlers
│   └── api/
│       ├── auth.py              # JWT verification + FastAPI dependency
│       ├── handler.py           # CRUD business logic (in-memory store)
│       ├── models.py            # Pydantic v2 request/response models
│       └── routes.py            # Router — all 5 endpoints
│
├── tests/                       # Pytest suite
│   ├── conftest.py              # Fixtures, JWT helpers, store isolation
│   ├── test_health.py           # Health endpoint — 8 tests
│   ├── test_auth.py             # JWT auth layer — 12 tests
│   ├── test_items.py            # Full CRUD lifecycle — 30 tests
│   └── test_models.py           # Pydantic validation — 29 tests
│
├── modal_app.py                 # Modal app, ASGI wrapper, local entrypoint
├── modal_common.py              # EnvConfig, container image, environment registry
├── swagger.yaml                 # OpenAPI 3.0 specification
│
└── .github/workflows/
    ├── app-testing.yml          # Reusable pytest CI (standalone + workflow_call)
    ├── modal-deploy.yml         # CI/CD: test → deploy gate
    └── docs.yml                 # Documentation build → GitHub Pages
```

---

## Tech Stack

| Layer | Technology | Role |
|---|---|---|
| Runtime | Python 3.11 | Application language |
| Web framework | FastAPI | Async HTTP, routing, dependency injection |
| Data validation | Pydantic v2 | Request/response models, type enforcement |
| Serverless platform | Modal | Container orchestration, autoscaling |
| Authentication | PyJWT | JWT encode/decode/verify |
| Testing | pytest + httpx | Unit + integration test suite |
| CI/CD | GitHub Actions (Node 24) | Automated test and deploy pipeline |
| Documentation | MkDocs Material | This site, hosted on GitHub Pages |
