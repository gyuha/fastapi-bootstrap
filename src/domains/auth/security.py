"""Auth security utilities.

Provides:
  * JWT access/refresh token creation and verification.
  * argon2 password hashing and verification.
  * ``require_permission(key)`` FastAPI dependency factory (RBAC).
  * ``get_current_user`` dependency for authenticated endpoints.

JWT strategy
------------
* Bearer header only — no cookies.
* Access token TTL: 15 minutes.
* Refresh token TTL: 7 days.
* ``jti`` (JWT ID) claim uniquely identifies each token.
* Redis blacklist: ``jti`` is stored on logout; checked on every access request.

argon2 configuration
--------------------
Uses ``passlib[argon2]`` with default time/memory/parallelism factors which
meet OWASP recommendations for web workloads.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_async_session
from core.exceptions import UnauthorizedError
from core.redis import get_redis_dep

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from domains.auth.models import User

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
REFRESH_TOKEN_EXPIRE_DAYS: int = 7
JWT_ALGORITHM: str = "HS256"
_ACCESS_TOKEN_RESERVED_CLAIMS: frozenset[str] = frozenset({"sub", "jti", "iat", "exp", "type"})


@dataclass(frozen=True, slots=True)
class AccessTokenContext:
    """Metadata extracted from the current verified access token."""

    jti: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Password hashing — argon2
# ---------------------------------------------------------------------------

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Return argon2 hash of *plain*."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return ``True`` if *plain* matches *hashed* argon2 hash."""
    return _pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _secret_key() -> str:
    return get_settings().jwt_secret_key.get_secret_value()


def _make_jti() -> str:
    """Generate a random JWT ID (jti) claim."""
    return str(uuid.uuid4())


def create_access_token(
    user_id: str | uuid.UUID,
    jti: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Encode a signed JWT access token.

    Parameters
    ----------
    user_id:
        Subject claim (``sub``).  Stored as a string.
    jti:
        Optional explicit JWT ID; generated if not provided.
    extra_claims:
        Additional claims merged into the payload. Reserved access-token claims
        (``sub``, ``jti``, ``iat``, ``exp``, ``type``) are ignored here so the
        generated token always carries canonical mandatory claims.

    Returns
    -------
    str
        Signed JWT string.
    """
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    _jti = jti or _make_jti()

    safe_extra_claims = {
        key: value
        for key, value in (extra_claims or {}).items()
        if key not in _ACCESS_TOKEN_RESERVED_CLAIMS
    }
    payload: dict[str, Any] = {
        **safe_extra_claims,
        "sub": str(user_id),
        "jti": _jti,
        "iat": now,
        "exp": expire,
        "type": "access",
    }

    return jwt.encode(payload, _secret_key(), algorithm=JWT_ALGORITHM)


def create_refresh_token(
    user_id: str | uuid.UUID,
    family_id: str | None = None,
) -> tuple[str, str, str]:
    """Create a refresh token.

    Returns
    -------
    tuple[raw_token, jti, family_id]
        ``raw_token`` — opaque random string (emailed / stored by client).
        ``jti``       — unique token identifier.
        ``family_id`` — rotation family (all tokens in one chain share it).
    """
    now = datetime.now(UTC)
    expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    _jti = _make_jti()
    _family_id = family_id or str(uuid.uuid4())
    raw = secrets.token_urlsafe(48)

    # We encode the raw token as a JWT just for convenience of returning
    # a single string to the client; the DB stores the SHA-256 hash.
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "jti": _jti,
        "iat": now,
        "exp": expire,
        "type": "refresh",
        "fid": _family_id,
        "raw": raw,
    }
    token_str = jwt.encode(payload, _secret_key(), algorithm=JWT_ALGORITHM)
    return token_str, _jti, _family_id


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT token.

    Raises
    ------
    UnauthorizedError
        If the token is expired or invalid.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _secret_key(),
            algorithms=[JWT_ALGORITHM],
        )
        return payload
    except ExpiredSignatureError as exc:
        raise UnauthorizedError("Token has expired.") from exc
    except JWTError as exc:
        raise UnauthorizedError(f"Invalid token: {exc}") from exc


def _jwt_numeric_date_to_datetime(value: object) -> datetime:
    """Convert a JWT NumericDate claim into an aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    raise UnauthorizedError("Access token is missing a valid exp claim.")


def decode_access_token_context(token: str) -> AccessTokenContext:
    """Return the current access token's jti and expiration timestamp."""
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise UnauthorizedError("Token is not an access token.")

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise UnauthorizedError("Access token is missing a valid jti claim.")

    return AccessTokenContext(
        jti=jti,
        expires_at=_jwt_numeric_date_to_datetime(payload.get("exp")),
    )


def hash_token(raw: str) -> str:
    """Return SHA-256 hex digest of *raw* — used for refresh/verification token storage."""
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Redis blacklist helpers
# ---------------------------------------------------------------------------

_BLACKLIST_PREFIX = "jwt:blacklist:"


def _seconds_until(expires_at: datetime) -> int:
    """Return a positive Redis TTL ending at *expires_at*."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    else:
        expires_at = expires_at.astimezone(UTC)
    return max(1, int((expires_at - datetime.now(UTC)).total_seconds()))


async def blacklist_jti(
    redis: Redis,
    jti: str,
    ttl_seconds: int | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Add *jti* to the Redis JWT blacklist until the token expires."""
    key = f"{_BLACKLIST_PREFIX}{jti}"
    _ttl = _seconds_until(expires_at) if expires_at is not None else ttl_seconds
    if _ttl is None:
        _ttl = ACCESS_TOKEN_EXPIRE_MINUTES * 60 + 60  # +1min buffer for legacy callers
    await redis.set(key, "1", ex=_ttl)
    logger.debug("jwt_blacklisted", jti=jti, ttl_seconds=_ttl)


async def is_jti_blacklisted(redis: Redis, jti: str) -> bool:
    """Return True if *jti* is in the Redis blacklist."""
    key = f"{_BLACKLIST_PREFIX}{jti}"
    return bool(await redis.exists(key))


# ---------------------------------------------------------------------------
# FastAPI security scheme
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


async def get_current_access_token_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AccessTokenContext:
    """FastAPI dependency — extract jti and exp from the current access token."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return decode_access_token_context(credentials.credentials)
    except UnauthorizedError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_async_session),
    redis: Redis = Depends(get_redis_dep),
) -> User:
    """FastAPI dependency — return the authenticated User or raise 401.

    Validates:
    1. Bearer token present in Authorization header.
    2. JWT signature + expiry.
    3. Token type == "access".
    4. jti NOT in Redis blacklist.
    5. User exists and is active.
    """
    # Late import to avoid circular dependency: models → security → models
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from domains.auth.models import Role, User

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = decode_token(token)
    except UnauthorizedError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token is missing a valid jti claim.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if await is_jti_blacklisted(redis, jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id_str: str = payload.get("sub", "")
    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject.",
        ) from exc

    result = await session.execute(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.roles).selectinload(Role.permissions))
    )
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive.",
        )

    return user


# ---------------------------------------------------------------------------
# RBAC dependency factory
# ---------------------------------------------------------------------------


def require_permission(key: str) -> Callable[..., Awaitable[User]]:
    """FastAPI dependency factory — enforce a permission key.

    Usage::

        @router.post("/messages", dependencies=[Depends(require_permission("chat:write"))])
        async def create_message(...):
            ...

    Raises
    ------
    HTTPException 401
        If the request is not authenticated.
    HTTPException 403
        If the authenticated user lacks the required permission.
    """

    async def _check(user: User = Depends(get_current_user)) -> User:
        if not user.has_permission(key):
            logger.warning(
                "rbac_denied",
                user_id=str(user.id),
                required_permission=key,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{key}' required.",
            )
        return user

    return _check
