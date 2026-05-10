# ============================================================
# Dockerfile — {{ cookiecutter.project_name }}
# ============================================================
# Multi-stage build:
#   uv-binary  → pinned uv binary donor
#   builder    → install ALL deps (incl. dev) via uv; build wheel;
#                create a /runtime-venv with ONLY runtime deps;
#                pre-compile bytecode inside that venv
#   runtime    → slim production image; copies /runtime-venv + alembic
#                from builder; ZERO dev tools (pytest, ruff, mypy absent)
#
# Local dev:
#   Do NOT build this image for daily development.
#   Run:  make dev  (infra via docker-compose + FastAPI on host with uv run)
#
# Staging / production:
#   docker build --target runtime -t {{ cookiecutter.project_slug }}:latest .
#   docker run --env-file .env -p {{ cookiecutter.fastapi_port }}:{{ cookiecutter.fastapi_port }} {{ cookiecutter.project_slug }}:latest
#
# NOTE: Commit uv.lock to your repository for reproducible builds.
#       Generate/update with:  uv lock
# ============================================================

# ── Shared build args ──────────────────────────────────────────────────────────
ARG PYTHON_VERSION={{ cookiecutter.python_version }}
ARG UV_VERSION=0.5.26

# ==============================================================================
# Stage 0 — uv binary donor (pinned version, copied into subsequent stages)
# ==============================================================================
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-binary

# ==============================================================================
# Stage 1 — builder
# ==============================================================================
# Full build environment:
#   • Creates /runtime-venv with ONLY [project].dependencies (no dev group).
#     This venv is the sole artifact copied into the production runtime stage.
#   • Also installs dev deps into system Python so CI can run tests here.
#   • Builds a distribution wheel via `uv build`.
#   • Pre-compiles .pyc files inside /runtime-venv for faster cold-start.
# ==============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# ── System build tooling ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

# ── Copy uv from the pinned uv image ──────────────────────────────────────────
COPY --from=uv-binary /uv /usr/local/bin/uv
COPY --from=uv-binary /uvx /usr/local/bin/uvx

# ── Build-time uv configuration ───────────────────────────────────────────────
#   UV_COMPILE_BYTECODE=1  → compile .pyc at install time (faster runtime start)
#   UV_CACHE_DIR           → cache location for BuildKit cache mount
#   PYTHONDONTWRITEBYTECODE=0 → allow .pyc creation
ENV UV_COMPILE_BYTECODE=1 \
    UV_CACHE_DIR=/root/.cache/uv \
    PYTHONDONTWRITEBYTECODE=0 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# ── Layer 1: dependency resolution (cached independently from source code) ────
# Only pyproject.toml and uv.lock are needed to resolve the dependency graph.
# Docker re-runs this layer only when those files change.
#
# uv.lock* copies uv.lock if present (first `uv lock` must be run locally).
# For reproducible production builds always commit uv.lock.
COPY pyproject.toml uv.lock* ./

# ── Layer 2: application source (changes more frequently) ─────────────────────
# README.md is listed in pyproject.toml[project].readme — hatchling needs it
# when building the wheel.
COPY README.md ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# ── Step A: create the runtime-only virtual environment ───────────────────────
# /runtime-venv receives ONLY [project].dependencies — the dev dependency-group
# (pytest, pytest-asyncio, ruff, mypy, fakeredis, type stubs …) is excluded.
# This venv is what gets copied verbatim into the production runtime stage.
RUN uv venv /runtime-venv

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        echo ">>> uv.lock found — runtime sync (--locked, --no-group dev)"; \
        VIRTUAL_ENV=/runtime-venv uv sync --no-group dev --no-install-project --locked; \
    else \
        echo ">>> uv.lock NOT found — runtime sync (no dev group, generating lock)"; \
        VIRTUAL_ENV=/runtime-venv uv sync --no-group dev --no-install-project; \
    fi

# ── Step B: build the distribution wheel ──────────────────────────────────────
# Produces a platform-independent .whl in /build/dist/.
# The wheel declares only runtime deps in its metadata (dev extras are absent).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /build/dist

# ── Step C: install app wheel into the runtime venv ───────────────────────────
# --no-deps: runtime deps are already in /runtime-venv (installed in Step A).
# Installing with --no-deps avoids any risk of pulling dev deps transitively.
RUN --mount=type=cache,target=/root/.cache/uv \
    VIRTUAL_ENV=/runtime-venv uv pip install --no-deps /build/dist/*.whl

# ── Step D: pre-compile bytecode inside the runtime venv ──────────────────────
# UV_COMPILE_BYTECODE=1 already compiled site-packages during uv install.
# `compileall` handles any remaining .py files that uv may have missed.
# -q  quiet (suppress per-file output)   -j0  use all CPU cores
RUN /runtime-venv/bin/python -m compileall -q -j0 /runtime-venv/lib/

# ── Step E: install ALL deps (incl. dev) into system Python for CI ────────────
# pytest, ruff, mypy, fakeredis, etc. are installed here for test runs.
# These packages NEVER reach the production image — they live only in the
# builder stage's system site-packages, which is discarded after the build.
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        echo ">>> Installing all deps (incl. dev) into system Python (builder only)"; \
        UV_SYSTEM_PYTHON=1 uv sync --all-groups --no-install-project --locked; \
    else \
        UV_SYSTEM_PYTHON=1 uv sync --all-groups --no-install-project; \
    fi

# Install app wheel into system Python so test imports resolve correctly.
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_SYSTEM_PYTHON=1 uv pip install --no-deps /build/dist/*.whl

# ==============================================================================
# Stage 2 — runtime  (slim production image)
# ==============================================================================
# What IS in this image:
#   ✓ python:slim-bookworm base (minimal OS)
#   ✓ libpq5    — required by asyncpg / psycopg2 C extensions at runtime
#   ✓ curl      — used by the HEALTHCHECK
#   ✓ uv binary — for running `alembic upgrade head` as a startup step
#   ✓ /runtime-venv  — fastapi, uvicorn, sqlalchemy, asyncpg, redis,
#                       passlib[argon2], python-jose, structlog, httpx,
#                       fastapi-mail, sse-starlette
#                       [+ langchain/langchain-litellm if include_chat_domain=yes]
#   ✓ /app/alembic   — migration scripts (needed for `alembic upgrade head`)
#   ✓ /app/alembic.ini — alembic configuration
#
# What is NOT in this image:
#   ✗ pytest, pytest-asyncio, pytest-cov
#   ✗ ruff, mypy, pre-commit
#   ✗ fakeredis, anyio (test helpers)
#   ✗ sqlalchemy[mypy], types-passlib, types-python-jose (type stubs)
#   ✗ build-essential, libpq-dev, git (compiler toolchain)
#   ✗ uv.lock, pyproject.toml, src/ (no editable install; wheel is in venv)
# ==============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# ── Runtime system dependencies ────────────────────────────────────────────────
# libpq5   — shared library for PostgreSQL client (asyncpg / psycopg2)
# curl     — HEALTHCHECK probe
# No build-essential or libpq-dev — C compilation never happens at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Copy uv for runtime operations (e.g. alembic migrations) ──────────────────
# uv itself is a static binary; no extra system libraries needed.
COPY --from=uv-binary /uv /usr/local/bin/uv

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN groupadd --gid 1000 appgroup && \
    useradd --uid 1000 --gid appgroup --no-create-home --shell /bin/bash appuser

WORKDIR /app

# ── Copy runtime venv from builder (NO dev tools) ─────────────────────────────
# This venv was created with `uv sync --no-group dev` — it contains only the
# packages declared in [project].dependencies in pyproject.toml.
# Dev tools (pytest, ruff, mypy, fakeredis …) are absent by construction.
COPY --from=builder /runtime-venv /runtime-venv

# ── Copy alembic migrations and config ────────────────────────────────────────
# alembic/env.py imports the app package from the runtime venv's site-packages.
# The src/ directory is NOT copied — the installed wheel provides the package.
COPY --from=builder /build/alembic/ ./alembic/
COPY --from=builder /build/alembic.ini ./alembic.ini

# ── Set correct ownership ─────────────────────────────────────────────────────
RUN chown -R appuser:appgroup /app /runtime-venv

USER appuser

# ── Runtime environment ───────────────────────────────────────────────────────
# VIRTUAL_ENV + PATH activate the runtime venv without `source activate`.
# PYTHONPATH is intentionally absent — the installed wheel is in site-packages.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=0 \
    VIRTUAL_ENV=/runtime-venv \
    PATH="/runtime-venv/bin:$PATH" \
    PORT={{ cookiecutter.fastapi_port }}

EXPOSE ${PORT}

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# ── Default command ───────────────────────────────────────────────────────────
# uvicorn is resolved via /runtime-venv/bin/uvicorn (PATH above).
# Override WORKERS in production (e.g. docker run -e WORKERS=4).
CMD ["sh", "-c", \
     "uvicorn {{ cookiecutter.package_name }}.main:app \
      --host 0.0.0.0 \
      --port ${PORT} \
      --workers ${WORKERS:-1} \
      --proxy-headers \
      --forwarded-allow-ips='*'"]
