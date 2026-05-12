"""SQLAlchemy async engine, session factory, and declarative base.

All domain models inherit from :data:`Base`.  The async engine is constructed
once from :attr:`~app.core.config.Settings.async_database_url`
and reused throughout the application lifetime.

Usage::

    # In domain models:
    from core.database import Base

    class User(Base):
        __tablename__ = "users"
        ...

    # In FastAPI route dependencies:
    from core.database import get_async_session

    async def some_route(session: AsyncSession = Depends(get_async_session)):
        ...

    # In Alembic env.py:
    from core.database import Base
    target_metadata = Base.metadata
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from core.config import settings

# ---------------------------------------------------------------------------
# Declarative base — all domain models extend this
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base.

    Extend all ORM models from this class so that Alembic autogenerate
    can discover them via :attr:`Base.metadata`.

    Example::

        from core.database import Base

        class User(Base):
            __tablename__ = "users"
            id: Mapped[int] = mapped_column(primary_key=True)
    """

    # Subclasses can override __abstract__ = True to skip table creation.
    pass


# ---------------------------------------------------------------------------
# Async engine
# ---------------------------------------------------------------------------


def _build_engine() -> AsyncEngine:
    """Create the async SQLAlchemy engine from application settings.

    Engine parameters are tuned for typical web workloads:
    * Pool size 5 with 10 overflow (production)
    * echo disabled by default; enable via APP_DEBUG for SQL logging
    """
    return create_async_engine(
        settings.async_database_url,
        echo=settings.app_debug,
        pool_pre_ping=True,  # detect stale connections
        pool_size=5,
        max_overflow=10,
        pool_recycle=3600,  # recycle connections after 1h
    )


engine: AsyncEngine = _build_engine()


# ---------------------------------------------------------------------------
# Async session factory
# ---------------------------------------------------------------------------

AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # avoid lazy-load errors after commit
    autoflush=False,
    autocommit=False,
)


# ---------------------------------------------------------------------------
# FastAPI dependency — yields a session and closes it after the request
# ---------------------------------------------------------------------------


async def get_async_session() -> AsyncGenerator[AsyncSession, Any]:
    """FastAPI dependency that yields an :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

    Rolls back on exception; always closes the session.

    Example::

        from fastapi import Depends
        from sqlalchemy.ext.asyncio import AsyncSession
        from core.database import get_async_session

        async def route(session: AsyncSession = Depends(get_async_session)):
            result = await session.execute(select(User))
            ...
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
