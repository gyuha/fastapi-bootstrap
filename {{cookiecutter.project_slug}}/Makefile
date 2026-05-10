# ============================================================
# {{ cookiecutter.project_name }} — Developer Makefile
# ============================================================
# Usage:
#   make dev           → spin up infra containers + start FastAPI with hot-reload
#   make infra         → start infra containers and wait until all healthy (60s)
#   make infra-health  → alias for infra; use in CI/bootstrap validation
#   make test          → run full pytest suite
#   make lint          → ruff check + mypy
#   make format        → ruff format + ruff check --fix
#   make migrate       → apply pending Alembic migrations
#   make revision      → create a new Alembic autogenerate revision
#   make install       → uv sync (install all deps incl. dev)
#   make clean         → remove build/cache artifacts
#
# Pre-requisites:
#   - uv    (https://docs.astral.sh/uv/)
#   - Docker + docker-compose (for infra services)
# ============================================================

# ── Variables ─────────────────────────────────────────────────────────────────
SHELL       := /bin/bash
.DEFAULT_GOAL := help

PROJECT     := {{ cookiecutter.project_slug }}
PACKAGE     := {{ cookiecutter.package_name }}
SRC_DIR     := src/$(PACKAGE)
TEST_DIR    := tests
HOST        := {{ cookiecutter.fastapi_host }}
PORT        := {{ cookiecutter.fastapi_port }}

# Compose file lives at project root
COMPOSE     := docker compose
COMPOSE_FILE := docker-compose.yml

# uv run wrapper — ensures we always use the project venv
UV          := uv run
UVPYTHON    := uv run python

# ── Phony targets ────────────────────────────────────────────────────────────
.PHONY: help install dev serve infra infra-down infra-health \
        test test-unit test-integration test-cov \
        lint format typecheck \
        migrate revision downgrade \
        smoke-test smoke-test-no-chat smoke-test-skip-verify \
        clean clean-docker \
        {% if cookiecutter.use_pre_commit == "yes" %}pre-commit-install pre-commit-run{% endif %}

# ── Help ──────────────────────────────────────────────────────────────────────
help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Setup ─────────────────────────────────────────────────────────────────────
install: ## Install all dependencies (including dev) via uv; copy .env if missing
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "📋  .env created from .env.example — edit SECRET_KEY and JWT_SECRET_KEY before production."; \
	fi
	uv sync
	{% if cookiecutter.use_pre_commit == "yes" %}
	$(UV) pre-commit install
	@echo "✅  pre-commit hooks installed (ruff + mypy run on staged files)."
	{% endif %}
	@echo "✅  Dependencies installed. Run 'make dev' to start the full stack."

# ── Infrastructure ────────────────────────────────────────────────────────────
infra: ## Start infrastructure containers and wait for healthy status (60s timeout)
	@bash scripts/wait_for_services.sh $(INFRA_TIMEOUT)

infra-health: ## Poll all containers until healthy (alias for CI/bootstrap validation)
	@bash scripts/wait_for_services.sh $(INFRA_TIMEOUT)

infra-down: ## Stop and remove infrastructure containers
	$(COMPOSE) -f $(COMPOSE_FILE) down

infra-logs: ## Follow docker-compose logs
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f

# Timeout override: make infra INFRA_TIMEOUT=120
INFRA_TIMEOUT ?= 60

# ── Dev server ────────────────────────────────────────────────────────────────
# Full bootstrap: uv sync → docker compose up -d (healthy) → alembic upgrade → uvicorn --reload
dev: install infra migrate ## Bootstrap: install deps + start infra + FastAPI hot-reload
	@echo ""
	@echo "🚀  Starting FastAPI at http://$(HOST):$(PORT)"
	@echo "     Docs       : http://$(HOST):$(PORT)/docs"
	@echo "     ReDoc      : http://$(HOST):$(PORT)/redoc"
	@echo "     Health     : http://$(HOST):$(PORT)/health"
	@echo "     Mailpit    : http://localhost:8025"
	@echo ""
	$(UV) uvicorn $(PACKAGE).main:app \
		--host $(HOST) \
		--port $(PORT) \
		--reload \
		--reload-dir $(SRC_DIR) \
		--log-level info

# Re-start dev server only (infra + deps already running — no install/migrate)
serve: ## Run FastAPI hot-reload without re-running infra or migrations
	@echo "🚀  Starting FastAPI at http://$(HOST):$(PORT) (no infra/migrate step)"
	$(UV) uvicorn $(PACKAGE).main:app \
		--host $(HOST) \
		--port $(PORT) \
		--reload \
		--reload-dir $(SRC_DIR) \
		--log-level info

# ── Testing ───────────────────────────────────────────────────────────────────
test: ## Run all tests with coverage
	$(UV) pytest $(TEST_DIR) -v

test-unit: ## Run only unit tests (no I/O)
	$(UV) pytest $(TEST_DIR) -v -m unit

test-integration: ## Run only integration tests (requires running infra)
	$(UV) pytest $(TEST_DIR) -v -m integration

test-cov: ## Run tests and open HTML coverage report
	$(UV) pytest $(TEST_DIR) --cov=$(SRC_DIR) --cov-report=html
	@open htmlcov/index.html 2>/dev/null || xdg-open htmlcov/index.html 2>/dev/null || true

test-fast: ## Run tests without coverage (faster feedback loop)
	$(UV) pytest $(TEST_DIR) -v --no-cov

# ── Code quality ──────────────────────────────────────────────────────────────
lint: ## Run ruff linter + mypy type checker
	@echo "── ruff check ──────────────────────────────"
	$(UV) ruff check $(SRC_DIR) $(TEST_DIR)
	@echo "── mypy ────────────────────────────────────"
	$(UV) mypy $(SRC_DIR)

format: ## Auto-format code (ruff format + ruff check --fix)
	@echo "── ruff format ─────────────────────────────"
	$(UV) ruff format $(SRC_DIR) $(TEST_DIR)
	@echo "── ruff check --fix ────────────────────────"
	$(UV) ruff check --fix $(SRC_DIR) $(TEST_DIR)

typecheck: ## Run mypy only
	$(UV) mypy $(SRC_DIR)

# ── Alembic migrations ────────────────────────────────────────────────────────
migrate: ## Apply all pending Alembic migrations (upgrade head)
	$(UV) alembic upgrade head

revision: ## Create a new autogenerate Alembic revision
	@read -p "Migration message: " msg; \
	$(UV) alembic revision --autogenerate -m "$$msg"

downgrade: ## Downgrade one migration step
	$(UV) alembic downgrade -1

migration-history: ## Show migration history
	$(UV) alembic history --verbose

migration-current: ## Show current migration version
	$(UV) alembic current

# ── Pre-commit ────────────────────────────────────────────────────────────────
{% if cookiecutter.use_pre_commit == "yes" %}
# Hooks run ruff (linter+formatter) on staged files, mypy on full src/ but
# only when src files are staged.  Both skip unmodified files automatically.
pre-commit-install: ## Install git pre-commit hooks (ruff + mypy on staged files)
	$(UV) pre-commit install
	@echo "✅  Pre-commit installed: ruff runs on staged files, mypy on src/ when src changes."

pre-commit-run: ## Run all pre-commit hooks on every file (CI / one-off audit)
	$(UV) pre-commit run --all-files

pre-commit-update: ## Update all pre-commit hook revisions to latest
	$(UV) pre-commit autoupdate
{% endif %}

# ── Clean ─────────────────────────────────────────────────────────────────────
clean: ## Remove Python cache, build artifacts, and test reports
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov dist build *.egg-info

clean-docker: ## Remove docker volumes (⚠️  destroys local DB data)
	$(COMPOSE) -f $(COMPOSE_FILE) down -v --remove-orphans

# ── Utilities ─────────────────────────────────────────────────────────────────
shell: ## Open an interactive Python shell with app context
	$(UVPYTHON) -c "from $(PACKAGE).core.config import settings; print('Settings loaded:', settings.app_env)"

routes: ## Print all registered API routes
	$(UVPYTHON) -c "from $(PACKAGE).main import app; [print(r.path, r.methods) for r in app.routes]"

health: ## Check health endpoint
	@curl -sf http://$(HOST):$(PORT)/health | python3 -m json.tool || \
		echo "❌  Server not responding at http://$(HOST):$(PORT)/health"

# ── Smoke tests ───────────────────────────────────────────────────────────────
smoke-test: ## Run API smoke tests (requires running server + infra)
	$(UVPYTHON) scripts/smoke_test.py --host $(HOST) --port $(PORT)

smoke-test-no-chat: ## Run smoke tests, skip chat/LLM steps (no LLM API key needed)
	$(UVPYTHON) scripts/smoke_test.py --host $(HOST) --port $(PORT) --skip-chat

smoke-test-skip-verify: ## Run smoke tests, skip email verification step
	$(UVPYTHON) scripts/smoke_test.py --host $(HOST) --port $(PORT) --skip-email-verify
