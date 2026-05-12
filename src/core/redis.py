"""Async Redis client and connection pool.

A single :class:`redis.asyncio.Redis` instance is created on first access
and shared across the entire application lifetime.  The connection pool is
closed gracefully during shutdown via :func:`close_redis_client`.

Redis is used for:
  * JWT blacklist (``jti`` strings with TTL = token expiry)
  * Refresh-token reuse detection
  * OAuth state nonce (short TTL)
  * Rate limiting
  * General cache
  * SSE fan-out pub/sub channel

Usage::

    # In a FastAPI dependency or service
    from core.redis import get_redis_client

    redis = await get_redis_client()
    await redis.set("key", "value", ex=60)

    # Or as a FastAPI dependency
    from fastapi import Depends
    from redis.asyncio import Redis
    from core.redis import get_redis_dep

    async def route(redis: Redis = Depends(get_redis_dep)):
        ...
"""

from __future__ import annotations

import structlog
from redis.asyncio import Redis, from_url
from redis.exceptions import ConnectionError as RedisConnectionError

from core.config import settings

logger = structlog.get_logger(__name__)

# Module-level singleton — lazily initialised on first ``get_redis_client`` call
_redis_client: Redis | None = None


async def get_redis_client() -> Redis:
    """Return (or create) the shared async Redis client.

    Creates the connection pool on first call.  Subsequent calls return the
    cached instance.  The pool is pre-warmed by calling ``ping()`` in the
    application lifespan so start-up failures are surfaced immediately.

    Returns
    -------
    redis.asyncio.Redis
        A shared client instance backed by a connection pool.
    """
    global _redis_client

    if _redis_client is None:
        try:
            _redis_client = await from_url(
                settings.redis_dsn,
                encoding="utf-8",
                decode_responses=True,
                max_connections=20,
            )
            logger.info("redis_pool_created", dsn=settings.redis_dsn.split("@")[-1])
        except RedisConnectionError as exc:
            logger.error("redis_connection_failed", error=str(exc))
            raise

    return _redis_client


async def close_redis_client() -> None:
    """Close the Redis connection pool.

    Called during application shutdown (see ``lifespan`` in ``main.py``).
    Safe to call even if the client was never initialised.
    """
    global _redis_client

    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
        logger.info("redis_pool_closed")


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_redis_dep() -> Redis:
    """FastAPI dependency that yields the shared Redis client.

    Usage::

        from fastapi import Depends
        from redis.asyncio import Redis
        from core.redis import get_redis_dep

        @router.get("/example")
        async def example(redis: Redis = Depends(get_redis_dep)):
            value = await redis.get("some-key")
            ...
    """
    return await get_redis_client()
