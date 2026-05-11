# ============================================================
# Dockerfile — FastAPI Bootstrap
# ============================================================
# Multi-stage build:
#   uv-binary  → pinned uv binary donor
#   builder    → install ALL deps (incl. dev) via uv; build wheel;
#                create a /runtime-venv with ONLY runtime deps;
#                pre-compile bytecode inside that venv
#   runtime    → slim production image; copies /runtime-venv + alembic
#                from builder; ZERO dev tools (pytest, ruff, mypy absent)
#
# Layer-caching strategy (builder stage):
#   1. Copy ONLY pyproject.toml + uv.lock → install deps (cached until deps change)
#   2. Copy source code → build & install wheel (only re-runs on code changes)
#
# Local dev:
#   Do NOT build this image for daily development.
#   Run:  make dev  (infra via docker-compose + FastAPI on host with uv run)
#
# Staging / production:
#   docker build --target runtime -t fastapi-bootstrap:latest .
#   docker run --env-file .env -p 8000:8000 fastapi-bootstrap:latest
#
# NOTE: Commit uv.lock to your repository for reproducible builds.
#       Generate/update with:  uv lock
# ============================================================

# ── Shared build args ──────────────────────────────────────────────────────────
ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.6.13

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
#
# Layer-caching design:
#   Layer A  pyproject.toml + uv.lock (dependency manifests, rarely change)
#            → uv sync (runtime deps into /runtime-venv)
#            → uv sync --all-groups (dev deps into system Python)
#            Both sync steps are cached until pyproject.toml or uv.lock change.
#
#   Layer B  README.md + src/ + alembic/ + alembic.ini (source, changes often)
#            → uv build --wheel
#            → uv pip install wheel into /runtime-venv + system Python
#            → compileall (bytecode pre-compilation)
# ==============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# ── System build tooling ───────────────────────────────────────────────────────
# build-essential + libpq-dev  → compile C extensions (asyncpg, cryptography)
# git                          → some packages resolve VCS sources at install time
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
#   UV_LINK_MODE=copy      → copy instead of hard-link (cross-filesystem safety)
#   PYTHONDONTWRITEBYTECODE=0 → allow .pyc creation inside venv
ENV UV_COMPILE_BYTECODE=1 \
    UV_CACHE_DIR=/root/.cache/uv \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=0 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# ──────────────────────────────────────────────────────────────────────────────
# Layer A — dependency resolution (cached independently from source code)
# ──────────────────────────────────────────────────────────────────────────────
# Only pyproject.toml and uv.lock are needed to resolve the dependency graph.
# Docker re-runs everything below ONLY when those files change.
#
# uv.lock* copies uv.lock if present (asterisk = optional glob in BuildKit).
# For reproducible production builds, always commit uv.lock.
# First-time users: run `uv lock` (or `uv sync`) to generate it locally.
COPY pyproject.toml uv.lock* ./

# ── Step A1: create the runtime-only virtual environment ──────────────────────
# /runtime-venv receives ONLY [project].dependencies — the dev dependency-group
# (pytest, pytest-asyncio, ruff, mypy, fakeredis, type stubs …) is excluded.
# This venv is what gets copied verbatim into the production runtime stage.
RUN uv venv /runtime-venv

# ── Step A2: sync runtime deps (no dev group, no project itself yet) ──────────
# --no-install-project: we install the wheel in Layer B after `uv build`.
# --locked:             use the pinned versions from uv.lock (if present).
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        echo ">>> uv.lock found — syncing runtime deps (--locked, --no-group dev)"; \
        VIRTUAL_ENV=/runtime-venv uv sync \
            --no-group dev \
            --no-install-project \
            --locked; \
    else \
        echo ">>> uv.lock NOT found — syncing runtime deps (no lock, no dev group)"; \
        echo "    Run 'uv lock' locally and commit uv.lock for reproducible builds."; \
        VIRTUAL_ENV=/runtime-venv uv sync \
            --no-group dev \
            --no-install-project; \
    fi

# ── Step A3: install ALL deps (incl. dev) into system Python for CI ───────────
# pytest, ruff, mypy, fakeredis, etc. are installed here for test runs.
# These packages NEVER reach the production image — they live only in the
# builder stage's system site-packages, which is discarded after the build.
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
        echo ">>> Syncing all deps (incl. dev) into system Python (builder only)"; \
        UV_SYSTEM_PYTHON=1 uv sync \
            --all-groups \
            --no-install-project \
            --locked; \
    else \
        UV_SYSTEM_PYTHON=1 uv sync \
            --all-groups \
            --no-install-project; \
    fi

# ──────────────────────────────────────────────────────────────────────────────
# Layer B — application source (changes more frequently than deps)
# ──────────────────────────────────────────────────────────────────────────────
# README.md is listed in pyproject.toml[project].readme — hatchling needs it
# when building the wheel.
COPY README.md ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# ── Step B1: build the distribution wheel ─────────────────────────────────────
# Produces a platform-independent .whl in /build/dist/.
# The wheel declares only runtime deps in its metadata (dev extras are absent).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --out-dir /build/dist

# ── Step B2: install app wheel into the runtime venv ──────────────────────────
# --no-deps: runtime deps are already in /runtime-venv (installed in Step A2).
# Installing with --no-deps avoids any risk of pulling dev deps transitively.
RUN --mount=type=cache,target=/root/.cache/uv \
    VIRTUAL_ENV=/runtime-venv uv pip install --no-deps /build/dist/*.whl

# ── Step B3: install app wheel into system Python (for CI test imports) ───────
# Dev deps are already in system Python from Step A3.
# The wheel must also be installed so `import fastapi_bootstrap` works in tests.
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_SYSTEM_PYTHON=1 uv pip install --no-deps /build/dist/*.whl

# ── Step B4: pre-compile bytecode inside the runtime venv ─────────────────────
# UV_COMPILE_BYTECODE=1 already compiled site-packages during uv install.
# `compileall` handles any remaining .py files that uv may have missed.
# -q  quiet (suppress per-file output)   -j0  use all CPU cores
RUN /runtime-venv/bin/python -m compileall -q -j0 /runtime-venv/lib/

# ==============================================================================
# Stage 2 — runtime  (slim production image)
# ==============================================================================
# Guiding principle: ZERO package-management tooling at runtime.
#   uv, pip, pip-tools are BUILD concerns — they have no place in a production
#   container.  All dependencies are pre-installed in /runtime-venv during the
#   builder stage; the runtime stage only *copies* that venv.  This eliminates
#   an entire class of supply-chain attack surface and keeps the image small.
#
# What IS in this image:
#   ✓ python:slim-bookworm base (minimal OS, no compiler toolchain)
#   ✓ libpq5    — shared library for PostgreSQL client (asyncpg / psycopg2)
#   ✓ curl      — used by the HEALTHCHECK
#   ✓ /runtime-venv  — fastapi, uvicorn, sqlalchemy, asyncpg, alembic, redis,
#                       passlib[argon2], python-jose, structlog, httpx,
#                       fastapi-mail, sse-starlette
#                       [+ langchain/langchain-litellm if include_chat_domain=yes]
#   ✓ /app/alembic   — migration scripts (run via /runtime-venv/bin/alembic)
#   ✓ /app/alembic.ini — alembic configuration
#
# What is NOT in this image:
#   ✗ uv, pip, pip-tools    — package managers (absent by design)
#   ✗ build-essential, libpq-dev, gcc, git (compiler / build toolchain)
#   ✗ pytest, pytest-asyncio, pytest-cov
#   ✗ ruff, mypy, pre-commit
#   ✗ fakeredis, anyio (test helpers)
#   ✗ sqlalchemy[mypy], types-passlib, types-python-jose (type stubs)
#   ✗ uv.lock, pyproject.toml, src/ (no editable install; wheel is in venv)
#
# Running migrations in production (no uv needed — alembic is in the venv):
#   docker run --rm --env-file .env fastapi-bootstrap:latest \
#       /runtime-venv/bin/alembic upgrade head
# ==============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="FastAPI Bootstrap" \
      org.opencontainers.image.description="Production-grade FastAPI backend with auth (JWT+OAuth+RBAC) and LLM chat proxy domains." \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.authors="Your Name <you@example.com>" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.url="http://localhost:8000"

# ── Runtime system dependencies ────────────────────────────────────────────────
# libpq5   — shared library for PostgreSQL client (asyncpg / psycopg2 C ext)
# curl     — HEALTHCHECK probe
# No build-essential, libpq-dev, or compiler toolchain — zero C compilation at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# NOTE: uv is intentionally NOT copied into this stage.
#       /runtime-venv contains all pre-compiled runtime dependencies.
#       Package management belongs in the builder stage, not production.

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
    PORT=8000

EXPOSE ${PORT}

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# ── Default command ───────────────────────────────────────────────────────────
# uvicorn is resolved via /runtime-venv/bin/uvicorn (PATH above).
# Override WORKERS in production (e.g. docker run -e WORKERS=4).
CMD ["sh", "-c", \
     "uvicorn fastapi_bootstrap.main:app \
      --host 0.0.0.0 \
      --port ${PORT} \
      --workers ${WORKERS:-1} \
      --proxy-headers \
      --forwarded-allow-ips='*'"]
