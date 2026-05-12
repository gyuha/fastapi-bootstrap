"""Alembic environment configuration.

Reads the database URL from the ``DATABASE_URL_SYNC`` environment variable
(or falls back to assembling it from the individual ``POSTGRES_*`` parts via
:class:`~app.core.config.Settings`).

Supports two run modes:
* **offline** — generates SQL migration scripts without a live DB connection.
* **online** — connects to the DB and applies migrations directly.

Both modes honour the same env-var lookup so that the same ``.env`` file
that configures the FastAPI application also drives Alembic.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Make the project package importable from alembic/
# ---------------------------------------------------------------------------
# alembic/env.py lives one level below the project root, so we need to add
# the src directory to sys.path so that the package can be imported.
_project_root = Path(__file__).resolve().parent.parent
_src_dir = _project_root / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

# ---------------------------------------------------------------------------
# Load .env file (if present) before importing settings
# ---------------------------------------------------------------------------
# We use python-dotenv-style parsing to honour the same .env file as uvicorn.
# pydantic-settings does this automatically when Settings is instantiated —
# but we call it here *before* the import so that DATABASE_URL_SYNC is
# available in os.environ for the URL lookup below.
try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]

    _dotenv_path = _project_root / ".env"
    if _dotenv_path.exists():
        load_dotenv(dotenv_path=_dotenv_path, override=False)
except ImportError:
    # python-dotenv not installed — rely on environment being pre-populated
    # (docker-compose, CI, or manually exported vars).
    pass

# ---------------------------------------------------------------------------
# Alembic Config object (wraps alembic.ini)
# ---------------------------------------------------------------------------
config = context.config

# Interpret the config file for Python logging when running alembic CLI.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

# ---------------------------------------------------------------------------
# Resolve the database URL
# ---------------------------------------------------------------------------
# Priority:
#   1. DATABASE_URL_SYNC env var (explicit sync DSN)
#   2. Settings.sync_database_url (assembled from POSTGRES_* vars)
#   3. DATABASE_URL env var (strip async driver prefix as fallback)


def _get_sync_db_url() -> str:
    """Return a sync (psycopg2) DSN for Alembic to use.

    Never returns an asyncpg DSN — Alembic requires a synchronous driver.
    """
    # ── 1. Explicit sync DSN ──────────────────────────────────────────────────
    url = os.environ.get("DATABASE_URL_SYNC", "").strip()
    if url:
        if "asyncpg" in url:
            logger.warning(
                "DATABASE_URL_SYNC contains 'asyncpg' — Alembic needs a sync driver. "
                "Use 'postgresql+psycopg2://' instead."
            )
        return url

    # ── 2. Assembled from Settings (reads POSTGRES_* vars) ───────────────────
    try:
        from core.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        return s.sync_database_url
    except Exception as exc:  # pragma: no cover
        logger.debug("Could not import Settings (%s) — falling back to env vars", exc)

    # ── 3. Fallback: convert async DATABASE_URL to sync ──────────────────────
    async_url = os.environ.get("DATABASE_URL", "").strip()
    if async_url:
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        sync_url = sync_url.replace("postgresql://", "postgresql+psycopg2://")
        logger.info("Using DATABASE_URL converted to sync driver: %s", sync_url)
        return sync_url

    raise RuntimeError(
        "No database URL configured for Alembic.\n"
        "Set DATABASE_URL_SYNC (preferred) or DATABASE_URL in your .env file.\n"
        "Example:\n"
        "  DATABASE_URL_SYNC=postgresql+psycopg2://"
        "app:app"
        "@localhost:5432"
        "/app_db"
    )


# Override the ini-level sqlalchemy.url with the env-resolved URL.
_db_url = _get_sync_db_url()
config.set_main_option("sqlalchemy.url", _db_url)

# ---------------------------------------------------------------------------
# Import all SQLAlchemy models so autogenerate can detect schema changes.
# ---------------------------------------------------------------------------
# The target_metadata is used by `alembic revision --autogenerate`.
# Import models here to populate the metadata before Alembic inspects it.
try:
    from core.database import Base

    # Import all domain models so Alembic autogenerate can detect them.
    # Add new model modules here when new domains are created.
    try:
        from domains.auth import models as _auth_models  # noqa: F401
    except ImportError:
        logger.debug("auth models not found — skipping")

    try:
        from domains.chat import models as _chat_models  # noqa: F401
    except ImportError:
        logger.debug("chat models not found — skipping")

    target_metadata = Base.metadata
except ImportError:
    # Database module may not be created yet (bootstrapping phase).
    # Autogenerate will work once the models are in place.
    logger.warning(
        "Could not import core.database — "
        "autogenerate will not detect schema changes until models are added."
    )
    target_metadata = None


# ---------------------------------------------------------------------------
# Offline mode — generate SQL scripts without connecting
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine.
    Calls to ``context.execute()`` emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Compare server defaults and types for more accurate autogenerate
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — connect and apply migrations directly
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an :class:`~sqlalchemy.engine.Engine` and associates a connection
    with the migration context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Accurate autogenerate: detect type and default changes
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------------
# Entry point — select mode automatically
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    logger.info("Running Alembic in offline mode (generating SQL scripts)")
    run_migrations_offline()
else:
    logger.info("Running Alembic in online mode (applying to DB at %s)", _db_url.split("@")[-1])
    run_migrations_online()
