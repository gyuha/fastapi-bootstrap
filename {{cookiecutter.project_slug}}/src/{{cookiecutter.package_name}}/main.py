"""FastAPI application factory.

Entry points for uvicorn:

    # Via Makefile (recommended — uses host/port from cookiecutter defaults)
    make dev                              # installs deps, starts infra, runs app
    make serve                            # hot-reload only (infra already running)

    # Explicit uvicorn (replaces <host>/<port> with your values)
    uv run uvicorn {{ cookiecutter.package_name }}.main:app \\
        --host {{ cookiecutter.fastapi_host }} --port {{ cookiecutter.fastapi_port }} \\
        --reload --reload-dir src/{{ cookiecutter.package_name }}

    # Direct module execution (host/port/reload read from Settings / .env)
    uv run python -m {{ cookiecutter.package_name }}                    # dev   — reload ON
    APP_ENV=production uv run python -m {{ cookiecutter.package_name }} # prod  — reload OFF

The ``app`` object is also importable for tests via the ``AsyncClient`` fixture.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from {{ cookiecutter.package_name }}.core.config import settings
from {{ cookiecutter.package_name }}.core.exceptions import register_exception_handlers
from {{ cookiecutter.package_name }}.core.logging import configure_logging
from {{ cookiecutter.package_name }}.core.middleware import CorrelationIdMiddleware

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager.

    Runs *startup* logic before yielding and *shutdown* logic after.
    """
    # ── Startup ──────────────────────────────────────────────────────────────
    configure_logging(level=settings.log_level, fmt=settings.log_format.value)
    logger.info(
        "starting_up",
        app="{{ cookiecutter.project_name }}",
        env=settings.app_env.value,
        host=settings.host,
        port=settings.port,
    )

    # Warm Redis connection pool
    from {{ cookiecutter.package_name }}.core.redis import get_redis_client  # noqa: PLC0415

    _redis = await get_redis_client()
    await _redis.ping()
    logger.info("redis_connected", url=settings.redis_dsn.split("@")[-1])

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    from {{ cookiecutter.package_name }}.core.redis import close_redis_client  # noqa: PLC0415

    await close_redis_client()
    logger.info("shutdown_complete", app="{{ cookiecutter.project_name }}")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def _get_user_key(request: Request) -> str:
    """Rate-limit key: use authenticated user ID if available, else remote IP."""
    # Try to extract user from request state (set by auth middleware / dependency)
    user = getattr(request.state, "user", None)
    if user is not None and hasattr(user, "id"):
        return f"user:{user.id}"
    return get_remote_address(request)


#: Shared Limiter instance — routers import this to apply per-route limits.
limiter = Limiter(key_func=_get_user_key)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        title="{{ cookiecutter.project_name }}",
        description="{{ cookiecutter.project_description }}",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ── Rate limiter state ────────────────────────────────────────────────────
    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # ── Middleware (outermost to innermost) ───────────────────────────────────
    # 1. Correlation-ID header injection + structlog context binding
    application.add_middleware(CorrelationIdMiddleware)

    # 2. CORS
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Correlation-ID"],
    )

    # ── Exception handlers ───────────────────────────────────────────────────
    register_exception_handlers(application)

    # ── Routers ──────────────────────────────────────────────────────────────
    _register_routers(application)

    return application


def _register_routers(application: FastAPI) -> None:
    """Register all domain routers and the health endpoint."""
    from fastapi import APIRouter  # noqa: PLC0415

    # Health check (no auth required)
    health_router = APIRouter(tags=["health"])

    @health_router.get("/health", summary="Health check")
    async def health() -> dict[str, str]:
        """Return service health status.

        Used by docker-compose healthcheck, load balancers, and ``make health``.

        Response body::

            {"status": "ok", "env": "development"}
        """
        return {"status": "ok", "env": settings.app_env.value}

    @health_router.get("/ready", summary="Readiness check")
    async def ready() -> dict[str, str]:
        """Return service readiness — verifies DB and Redis are reachable.

        Returns 200 when all dependencies respond; 503 if any are unavailable.
        Used by k8s readiness probes and smoke tests.
        """
        from fastapi import Response  # noqa: PLC0415

        checks: dict[str, str] = {}

        # Redis check
        try:
            from {{ cookiecutter.package_name }}.core.redis import get_redis_client  # noqa: PLC0415

            redis = await get_redis_client()
            await redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"error: {exc}"

        all_ok = all(v == "ok" for v in checks.values())
        return {"status": "ready" if all_ok else "degraded", **checks}

    application.include_router(health_router)

    # Auth domain
    try:
        from {{ cookiecutter.package_name }}.domains.auth.router import router as auth_router  # noqa: PLC0415

        application.include_router(auth_router, prefix="/api/v1")
        logger.debug("router_registered", prefix="/api/v1/auth")
    except ImportError:
        logger.debug("auth_router_not_found", note="Will be added in later phase")

    {% if cookiecutter.include_chat_domain == "yes" %}
    # Chat domain
    try:
        from {{ cookiecutter.package_name }}.domains.chat.router import router as chat_router  # noqa: PLC0415

        application.include_router(chat_router, prefix="/api/v1")
        logger.debug("router_registered", prefix="/api/v1/chat")
    except ImportError:
        logger.debug("chat_router_not_found", note="Will be added in later phase")
    {% endif %}


# ---------------------------------------------------------------------------
# Module-level ``app`` — uvicorn entry point
# ---------------------------------------------------------------------------

app: FastAPI = create_app()


# ---------------------------------------------------------------------------
# Direct execution — ``python -m {{ cookiecutter.package_name }}``
# ---------------------------------------------------------------------------
# Activates uvicorn hot-reload when APP_ENV=development (the default).
#
# Usage:
#   uv run python -m {{ cookiecutter.package_name }}          # dev   — reload ON
#   APP_ENV=production uv run python -m {{ cookiecutter.package_name }}  # prod  — reload OFF
#
# Preferred production invocation (multiple workers):
#   uv run uvicorn {{ cookiecutter.package_name }}.main:app --workers 4
#
# Note: uvicorn's --reload is incompatible with workers > 1; this block
#       forces workers=1 when reload is enabled.

if __name__ == "__main__":
    import uvicorn  # noqa: PLC0415 — intentional late import for __main__ only
    from pathlib import Path  # noqa: PLC0415

    _src_dir = Path(__file__).parent  # src/{{ cookiecutter.package_name }}/
    _reload = settings.is_development()

    uvicorn.run(
        "{{ cookiecutter.package_name }}.main:app",
        host=settings.host,
        port=settings.port,
        reload=_reload,
        # Scope file-watching to the package source tree; avoids spurious
        # reloads triggered by test output, htmlcov, or .env changes.
        reload_dirs=[str(_src_dir)] if _reload else None,
        log_level=settings.log_level.lower(),
        # reload and workers > 1 are mutually exclusive in uvicorn
        workers=1 if _reload else settings.workers,
    )
