# ============================================================
# FastAPI Bootstrap — Developer Makefile
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

PROJECT          := fastapi-bootstrap
PACKAGE          := fastapi_bootstrap
SRC_DIR          := src/$(PACKAGE)
TEST_DIR         := tests
HOST             := 0.0.0.0
PORT             := 8000
# User-facing display host — 0.0.0.0 binds all interfaces; browsers need localhost
DISPLAY_HOST     := $(if $(filter 0.0.0.0,$(HOST)),localhost,$(HOST))
POSTGRES_PORT    := 5432
REDIS_PORT       := 6379
MAILPIT_SMTP_PORT := 1025
MAILPIT_UI_PORT  := 8025

# Compose file lives at project root
COMPOSE     := docker compose
COMPOSE_FILE := docker-compose.yml

# uv run wrapper — ensures we always use the project venv
UV          := uv run
UVPYTHON    := uv run python

# ── Phony targets ────────────────────────────────────────────────────────────
.PHONY: help install dev serve infra infra-down infra-health \
        test test-unit test-integration test-mailpit-signup test-cov \
        lint format typecheck \
        migrate revision downgrade \
        health ready smoke-test smoke-test-no-chat smoke-test-skip-verify \
        clean clean-docker \
        prod-up prod-down prod-logs prod-build prod-migrate prod-health \
        pre-commit-install pre-commit-run pre-commit-update secrets-baseline

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
	
	$(UV) pre-commit install
	@echo "✅  pre-commit hooks installed (ruff + mypy run on staged files)."
	
	@echo "✅  Dependencies installed. Run 'make dev' to start the full stack."

# ── Infrastructure ────────────────────────────────────────────────────────────
infra: ## Start infrastructure containers (docker compose up -d) and wait for healthy status
	@COMPOSE_FILE=$(COMPOSE_FILE) bash scripts/wait_for_services.sh $(INFRA_TIMEOUT)

infra-health: ## Poll all containers until healthy (alias for CI/bootstrap validation)
	@COMPOSE_FILE=$(COMPOSE_FILE) bash scripts/wait_for_services.sh $(INFRA_TIMEOUT)

infra-down: ## Stop and remove infrastructure containers
	$(COMPOSE) -f $(COMPOSE_FILE) down

infra-logs: ## Follow docker-compose logs
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f

# Timeout override: make infra INFRA_TIMEOUT=120
INFRA_TIMEOUT ?= 60

# ── Dev server ────────────────────────────────────────────────────────────────
# Full bootstrap: uv sync → docker compose up -d (healthy) → alembic upgrade → uvicorn --reload
dev: install migrate ## Bootstrap: install deps + start infra + FastAPI hot-reload
	@echo ""
	@echo "🚀  Starting FastAPI at http://$(DISPLAY_HOST):$(PORT)"
	@echo "     Docs       : http://$(DISPLAY_HOST):$(PORT)/docs"
	@echo "     ReDoc      : http://$(DISPLAY_HOST):$(PORT)/redoc"
	@echo "     Health     : http://$(DISPLAY_HOST):$(PORT)/health"
	@echo "     Mailpit    : http://localhost:$(MAILPIT_UI_PORT)"
	@echo ""
	$(UV) uvicorn $(PACKAGE).main:app \
		--host $(HOST) \
		--port $(PORT) \
		--reload \
		--reload-dir $(SRC_DIR) \
		--log-level info

# Re-start dev server only (infra + deps already running — no install/migrate)
serve: ## Run FastAPI hot-reload without re-running infra or migrations
	@echo "🚀  Starting FastAPI at http://$(DISPLAY_HOST):$(PORT) (no infra/migrate step)"
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

test-mailpit-signup: ## Run live signup → Mailpit check (requires running FastAPI + Mailpit)
	RUN_MAILPIT_INTEGRATION=1 $(UV) pytest tests/auth/test_signup_mailpit_integration.py -v --no-cov

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
migrate: infra-health ## Start local infra if needed, then apply all pending Alembic migrations (upgrade head)
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

# Hooks run ruff (linter+formatter) on staged files, mypy on full src/ but
# only when src files are staged.  Both skip unmodified files automatically.
pre-commit-install: ## Install git pre-commit hooks (ruff + mypy on staged files)
	$(UV) pre-commit install
	@echo "✅  Pre-commit installed: ruff runs on staged files, mypy on src/ when src changes."

pre-commit-run: ## Run all pre-commit hooks on every file (CI / one-off audit)
	$(UV) pre-commit run --all-files

pre-commit-update: ## Update all pre-commit hook revisions to latest
	$(UV) pre-commit autoupdate

# Regenerate the detect-secrets baseline from scratch.
# Run this whenever you intentionally add new placeholder secrets to source
# files, or after the first clone when the baseline needs to be refreshed.
#   Usage: make secrets-baseline
secrets-baseline: ## Regenerate .secrets.baseline (detect-secrets full scan)
	$(UV) detect-secrets scan \
		--exclude-files '\.env\.example' \
		--exclude-files '\.secrets\.baseline' \
		> .secrets.baseline
	@echo "✅  .secrets.baseline updated — review changes with: git diff .secrets.baseline"


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

health: ## Check liveness endpoint
	@curl -sf http://$(DISPLAY_HOST):$(PORT)/health | python3 -m json.tool || \
		echo "❌  Server not responding at http://$(DISPLAY_HOST):$(PORT)/health"

ready: ## Check readiness endpoint (PostgreSQL + Redis + Mailpit)
	@curl -sf http://$(DISPLAY_HOST):$(PORT)/ready | python3 -m json.tool || \
		echo "❌  Dependencies not ready at http://$(DISPLAY_HOST):$(PORT)/ready"

# ── Smoke tests ───────────────────────────────────────────────────────────────
smoke-test: ## Run API smoke tests (requires running server + infra)
	$(UVPYTHON) scripts/smoke_test.py --host $(HOST) --port $(PORT)

smoke-test-no-chat: ## Run smoke tests, skip chat/LLM steps (no LLM API key needed)
	$(UVPYTHON) scripts/smoke_test.py --host $(HOST) --port $(PORT) --skip-chat

smoke-test-skip-verify: ## Run smoke tests, skip email verification step
	$(UVPYTHON) scripts/smoke_test.py --host $(HOST) --port $(PORT) --skip-email-verify

# ── Production (docker-compose.prod.yml overlay) ──────────────────────────────
#
#  필수 사전 작업:
#    cp .env.prod.example .env.prod
#    # 시크릿 생성
#    openssl rand -hex 32   # → SECRET_KEY
#    openssl rand -hex 32   # → JWT_SECRET_KEY
#    # .env.prod에 DB 자격증명, SMTP, LLM API 키 등 입력
#
# 적용되는 프로덕션 설정 (docker-compose.prod.yml):
#   • restart: always        — 장애 시 자동 재시작
#   • env_file: .env.prod    — 프로덕션 시크릿 분리
#   • volumes: []            — dev 전용 소스코드 마운트 제거
#   • Dockerfile --target runtime — multi-stage 빌드 런타임 스테이지
#   • mailpit 제외           — 실제 SMTP 사용
# ─────────────────────────────────────────────────────────────────────────────
PROD_COMPOSE_FILES := -f docker-compose.yml -f docker-compose.prod.yml
PROD_PROFILE      := --profile prod
PROD_COMPOSE      := $(COMPOSE) $(PROD_COMPOSE_FILES) $(PROD_PROFILE)

prod-up: ## Start production stack (postgres + redis + app; no mailpit)
	@if [ ! -f .env.prod ]; then \
		echo "❌  .env.prod 파일이 없습니다."; \
		echo "    cp .env.prod.example .env.prod"; \
		echo "    # 그 후 SECRET_KEY, JWT_SECRET_KEY, DB 비밀번호, SMTP 설정 등을 채우세요."; \
		exit 1; \
	fi
	$(PROD_COMPOSE) up -d --build
	@echo ""
	@echo "🚀  Production stack started."
	@echo "     Health: http://$(DISPLAY_HOST):$(PORT)/health"
	@echo "     Logs  : make prod-logs"

prod-down: ## Stop and remove production containers
	$(COMPOSE) $(PROD_COMPOSE_FILES) down

prod-logs: ## Follow production service logs
	$(COMPOSE) $(PROD_COMPOSE_FILES) logs -f

prod-build: ## Build production Docker image only (--target runtime)
	docker build \
		--target runtime \
		--tag $(PROJECT):prod \
		--build-arg PYTHON_VERSION="3.12" \
		.
	@echo "✅  Image built: $(PROJECT):prod"
	@docker images $(PROJECT):prod 2>/dev/null || true

prod-migrate: ## Run Alembic migrations in production container
	@if [ ! -f .env.prod ]; then \
		echo "❌  .env.prod 파일이 없습니다. cp .env.prod.example .env.prod"; \
		exit 1; \
	fi
	$(COMPOSE) $(PROD_COMPOSE_FILES) run --rm app alembic upgrade head

prod-health: ## Check production app health endpoint
	@curl -sf http://$(DISPLAY_HOST):$(PORT)/health | python3 -m json.tool || \
		echo "❌  Production server not responding at http://$(DISPLAY_HOST):$(PORT)/health"
