"""FastAPI application factory.

Entry points for uvicorn:

    # Via Makefile (recommended — uses host/port from cookiecutter defaults)
    make dev                              # installs deps, starts infra, runs app
    make serve                            # hot-reload only (infra already running)

    # Explicit uvicorn (replaces <host>/<port> with your values)
    uv run uvicorn app.main:app \\
        --host 0.0.0.0 --port 8000 \\
        --reload --reload-dir src/app

    # Direct module execution (host/port/reload read from Settings / .env)
    uv run python -m app                    # dev   — reload ON
    APP_ENV=production uv run python -m app # prod  — reload OFF

The ``app`` object is also importable for tests via the ``AsyncClient`` fixture.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from core.config import settings
from core.exceptions import register_exception_handlers
from core.logging import configure_logging
from core.middleware import CorrelationIdMiddleware

logger = structlog.get_logger(__name__)


async def _await_if_needed(value: Any) -> Any:
    """Await redis-py results only when the installed client returns an awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


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
        app="FastAPI Bootstrap",
        env=settings.app_env.value,
        host=settings.host,
        port=settings.port,
    )

    # Warm Redis connection pool
    from core.redis import get_redis_client

    _redis = await get_redis_client()
    await _await_if_needed(_redis.ping())
    logger.info("redis_connected", url=settings.redis_dsn.split("@")[-1])

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    from core.redis import close_redis_client

    await close_redis_client()
    logger.info("shutdown_complete", app="FastAPI Bootstrap")


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
        title="FastAPI Bootstrap",
        description=(
            "Production-grade FastAPI backend with auth (JWT+OAuth+RBAC) "
            "and LLM chat proxy domains."
        ),
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
    from fastapi import APIRouter

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
    async def ready(response: Response) -> dict[str, str]:
        """Return service readiness — verifies DB, Redis, and SMTP are reachable.

        Local development runs FastAPI on the host while docker-compose exposes
        PostgreSQL, Redis, and Mailpit on localhost. This endpoint performs real
        network checks against those configured endpoints so ``make smoke-test``
        can prove the host-run app can reach the local infra stack.

        Returns 200 when all dependencies respond; 503 if any are unavailable.
        """
        checks: dict[str, str] = {}

        async def _check_database() -> None:
            from sqlalchemy import text

            from core.database import engine

            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

        async def _check_redis() -> None:
            from core.redis import get_redis_client

            redis = await get_redis_client()
            await _await_if_needed(redis.ping())

        async def _check_mailpit_smtp() -> None:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(settings.mail_server, settings.mail_port),
                timeout=3,
            )
            banner = await asyncio.wait_for(reader.readline(), timeout=3)
            if not banner.startswith(b"220"):
                raise RuntimeError("SMTP server did not return a 220 banner")
            writer.write(b"QUIT\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        for name, check in (
            ("postgres", _check_database),
            ("redis", _check_redis),
            ("mailpit", _check_mailpit_smtp),
        ):
            try:
                await check()
                checks[name] = "ok"
            except Exception as exc:
                checks[name] = f"error: {exc}"

        all_ok = all(v == "ok" for v in checks.values())
        if not all_ok:
            response.status_code = 503
        return {"status": "ready" if all_ok else "degraded", **checks}

    application.include_router(health_router)

    # Auth domain
    try:
        from domains.auth.router import router as auth_router

        application.include_router(auth_router, prefix="/api/v1")
        logger.debug("router_registered", prefix="/api/v1/auth")
    except ImportError:
        logger.debug("auth_router_not_found", note="Will be added in later phase")

    # Chat domain
    try:
        from domains.chat.router import router as chat_router

        application.include_router(chat_router, prefix="/api/v1")
        logger.debug("router_registered", prefix="/api/v1/chat")
    except ImportError:
        logger.debug("chat_router_not_found", note="Will be added in later phase")


# ---------------------------------------------------------------------------
# Module-level ``app`` — uvicorn entry point
# ---------------------------------------------------------------------------

app: FastAPI = create_app()


# ---------------------------------------------------------------------------
# Direct execution — ``python -m app``
# ---------------------------------------------------------------------------
# Activates uvicorn hot-reload when APP_ENV=development (the default).
#
# Usage:
#   uv run python -m app          # dev   — reload ON
#   APP_ENV=production uv run python -m app  # prod  — reload OFF
#
# Preferred production invocation (multiple workers):
#   uv run uvicorn app.main:app --workers 4
#
# Note: uvicorn's --reload is incompatible with workers > 1; this block
#       forces workers=1 when reload is enabled.

if __name__ == "__main__":
    from pathlib import Path

    import uvicorn

    _src_dir = Path(__file__).parent  # src/app/
    _reload = settings.is_development()

    uvicorn.run(
        "\1",
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
