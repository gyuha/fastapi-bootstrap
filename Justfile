# ============================================================
# FastAPI Bootstrap — Justfile
# ============================================================
# Usage:
#   just dev                → Bootstrap: install deps + start infra + FastAPI hot-reload
#   just serve              → Run FastAPI hot-reload without re-running infra or migrations
#   just infra              → Start infra containers and wait until all healthy (60 s)
#   just infra-down         → Stop and remove infra containers
#   just test               → Run full pytest suite
#   just lint               → ruff check + mypy
#   just format             → ruff format + ruff check --fix
#   just migrate            → Apply pending Alembic migrations
#   MSG="my message" just revision → Create a new Alembic autogenerate revision
#   just install            → uv sync (install all deps incl. dev)
#   just clean              → Remove build/cache artifacts
#
# Pre-requisites:
#   - just  (https://github.com/casey/just)
#   - uv    (https://docs.astral.sh/uv/)
#   - Docker + docker-compose (for infra services)
# ============================================================

# Keep recipes free of Just/Jinja-style double-brace interpolation so generated
# projects can be scanned for unresolved Cookiecutter/Jinja placeholders with a
# simple delimiter check. Values below are rendered once by Cookiecutter.

# ── Variables ─────────────────────────────────────────────────────────────────
project           := "fastapi-bootstrap"
package           := "fastapi_bootstrap"
src_dir           := "src/fastapi_bootstrap"
test_dir          := "tests"
host              := "0.0.0.0"
port              := "8000"
display_host      := "localhost"
postgres_port     := "5432"
redis_port        := "6379"
mailpit_smtp_port := "1025"
mailpit_ui_port   := "8025"
compose_file      := "docker-compose.yml"
infra_timeout     := "60"

# ── Default recipe ────────────────────────────────────────────────────────────
[private]
default:
    @just --list

# ── Setup ─────────────────────────────────────────────────────────────────────
# Install all dependencies (including dev) via uv; copy .env if missing
install:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f .env ]; then
        cp .env.example .env
        echo "📋  .env created from .env.example — edit SECRET_KEY and JWT_SECRET_KEY before production."
    fi
    uv sync
    uv run pre-commit install
    echo "✅  pre-commit hooks installed (ruff + mypy run on staged files)."
    echo "✅  Dependencies installed. Run 'just dev' to start the full stack."

# ── Infrastructure ────────────────────────────────────────────────────────────
# Start infrastructure containers (docker compose up -d) and wait for healthy status
infra:
    COMPOSE_FILE=docker-compose.yml bash scripts/wait_for_services.sh 60

# Poll all containers until healthy (alias for CI/bootstrap validation)
infra-health:
    COMPOSE_FILE=docker-compose.yml bash scripts/wait_for_services.sh 60

# Stop and remove infrastructure containers
infra-down:
    docker compose -f docker-compose.yml down

# Follow docker-compose logs
infra-logs:
    docker compose -f docker-compose.yml logs -f

# ── Dev server ────────────────────────────────────────────────────────────────
# Bootstrap: install deps + start infra + apply migrations + FastAPI hot-reload
dev: install migrate
    @echo ""
    @echo "🚀  Starting FastAPI at http://localhost:8000"
    @echo "     Docs       : http://localhost:8000/docs"
    @echo "     ReDoc      : http://localhost:8000/redoc"
    @echo "     Health     : http://localhost:8000/health"
    @echo "     Mailpit    : http://localhost:8025"
    @echo ""
    uv run uvicorn fastapi_bootstrap.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --reload \
        --reload-dir src/fastapi_bootstrap \
        --log-level info

# Re-start dev server only (infra + deps already running — no install/migrate)
serve:
    @echo "🚀  Starting FastAPI at http://localhost:8000 (no infra/migrate step)"
    uv run uvicorn fastapi_bootstrap.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --reload \
        --reload-dir src/fastapi_bootstrap \
        --log-level info

# ── Testing ───────────────────────────────────────────────────────────────────
# Run all tests with coverage
test:
    uv run pytest tests -v

# Run only unit tests (no I/O)
test-unit:
    uv run pytest tests -v -m unit

# Run only integration tests (requires running infra)
test-integration:
    uv run pytest tests -v -m integration

# Run tests and open HTML coverage report
test-cov:
    uv run pytest tests --cov=src/fastapi_bootstrap --cov-report=html
    open htmlcov/index.html 2>/dev/null || xdg-open htmlcov/index.html 2>/dev/null || true

# Run tests without coverage (faster feedback loop)
test-fast:
    uv run pytest tests -v --no-cov

# ── Code quality ──────────────────────────────────────────────────────────────
# Run ruff linter + mypy type checker
lint:
    @echo "── ruff check ──────────────────────────────"
    uv run ruff check src/fastapi_bootstrap tests
    @echo "── mypy ────────────────────────────────────"
    uv run mypy src/fastapi_bootstrap

# Auto-format code (ruff format + ruff check --fix)
format:
    @echo "── ruff format ─────────────────────────────"
    uv run ruff format src/fastapi_bootstrap tests
    @echo "── ruff check --fix ────────────────────────"
    uv run ruff check --fix src/fastapi_bootstrap tests

# Run mypy only
typecheck:
    uv run mypy src/fastapi_bootstrap

# ── Alembic migrations ────────────────────────────────────────────────────────
# Start local infra if needed, then apply all pending Alembic migrations (upgrade head)
migrate: infra-health
    uv run alembic upgrade head

# Create a new autogenerate Alembic revision. Use MSG="my message" just revision,
# or omit MSG to be prompted interactively.
revision:
    #!/usr/bin/env bash
    set -euo pipefail
    msg="${MSG:-}"
    if [ -z "$msg" ]; then
        read -rp "Migration message: " msg
    fi
    uv run alembic revision --autogenerate -m "$msg"

# Downgrade one migration step
downgrade:
    uv run alembic downgrade -1

# Show migration history
migration-history:
    uv run alembic history --verbose

# Show current migration version
migration-current:
    uv run alembic current


# ── Pre-commit ────────────────────────────────────────────────────────────────
# Install git pre-commit hooks (ruff + mypy on staged files)
pre-commit-install:
    uv run pre-commit install
    @echo "✅  Pre-commit installed: ruff runs on staged files, mypy on src/ when src changes."

# Run all pre-commit hooks on every file (CI / one-off audit)
pre-commit-run:
    uv run pre-commit run --all-files

# Update all pre-commit hook revisions to latest
pre-commit-update:
    uv run pre-commit autoupdate

# Regenerate .secrets.baseline (detect-secrets full scan)
secrets-baseline:
    uv run detect-secrets scan \
        --exclude-files '\.env\.example' \
        --exclude-files '\.secrets\.baseline' \
        > .secrets.baseline
    @echo "✅  .secrets.baseline updated — review changes with: git diff .secrets.baseline"


# ── Clean ─────────────────────────────────────────────────────────────────────
# Remove Python cache, build artifacts, and test reports
clean:
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
    find . -type f -name "*.pyo" -delete 2>/dev/null || true
    find . -type f -name ".coverage" -delete 2>/dev/null || true
    rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov dist build *.egg-info

# Remove docker volumes (⚠️  destroys local DB data)
clean-docker:
    docker compose -f docker-compose.yml down -v --remove-orphans

# ── Utilities ─────────────────────────────────────────────────────────────────
# Open an interactive Python shell with app context
shell:
    uv run python -c "from fastapi_bootstrap.core.config import settings; print('Settings loaded:', settings.app_env)"

# Print all registered API routes
routes:
    uv run python -c "from fastapi_bootstrap.main import app; [print(r.path, r.methods) for r in app.routes]"

# Check liveness endpoint
health:
    @curl -sf http://localhost:8000/health | python3 -m json.tool || \
        echo "❌  Server not responding at http://localhost:8000/health"

# Check readiness endpoint (PostgreSQL + Redis + Mailpit)
ready:
    @curl -sf http://localhost:8000/ready | python3 -m json.tool || \
        echo "❌  Dependencies not ready at http://localhost:8000/ready"

# ── Smoke tests ───────────────────────────────────────────────────────────────
# Run API smoke tests (requires running server + infra)
smoke-test:
    uv run python scripts/smoke_test.py --host 0.0.0.0 --port 8000

# Run smoke tests, skip chat/LLM steps (no LLM API key needed)
smoke-test-no-chat:
    uv run python scripts/smoke_test.py --host 0.0.0.0 --port 8000 --skip-chat

# Run smoke tests, skip email verification step
smoke-test-skip-verify:
    uv run python scripts/smoke_test.py --host 0.0.0.0 --port 8000 --skip-email-verify
