"""Current access-token context extraction tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from jose import jwt

from core.config import get_settings
from core.database import get_async_session
from core.redis import get_redis_dep
from domains.auth import security
from domains.auth.router import _get_service, router
from domains.auth.security import create_access_token


class CapturingRedis:
    """Redis test double that records SET calls and key existence."""

    def __init__(self) -> None:
        self.set_calls: list[dict[str, Any]] = []
        self._values: dict[str, Any] = {}

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.set_calls.append({"key": key, "value": value, "ex": ex})
        self._values[key] = value

    async def exists(self, key: str) -> int:
        return int(key in self._values)


class FakeLogoutService:
    """Route-level fake that records logout token metadata."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def logout(
        self,
        refresh_token: str,
        access_jti: str | None = None,
        access_expires_at: datetime | None = None,
    ) -> None:
        self.calls.append(
            {
                "refresh_token": refresh_token,
                "access_jti": access_jti,
                "access_expires_at": access_expires_at,
            }
        )


class ExplodingSession:
    """Session test double that fails if token validation reaches database lookup."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("database lookup must not run for malformed access token")


@pytest.fixture
def logout_service() -> FakeLogoutService:
    return FakeLogoutService()


@pytest.fixture
def app(logout_service: FakeLogoutService) -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[_get_service] = lambda: logout_service
    application.dependency_overrides[security.get_current_user] = lambda: object()
    return application


def test_create_access_token_generates_unique_jti_claim() -> None:
    token_a = create_access_token("00000000-0000-4000-8000-000000000001")
    token_b = create_access_token("00000000-0000-4000-8000-000000000001")

    payload_a = security.decode_token(token_a)
    payload_b = security.decode_token(token_b)

    assert isinstance(payload_a["jti"], str)
    assert payload_a["jti"]
    assert isinstance(payload_b["jti"], str)
    assert payload_b["jti"]
    assert payload_a["jti"] != payload_b["jti"]


def test_decode_access_token_context_returns_jti_and_exp() -> None:
    decoder = getattr(security, "decode_access_token_context", None)
    assert callable(decoder), "security.decode_access_token_context must be implemented"

    before = datetime.now(UTC)
    token = create_access_token("00000000-0000-4000-8000-000000000001", jti="access-jti-123")

    context = decoder(token)

    assert context.jti == "access-jti-123"
    assert context.expires_at.tzinfo is not None
    assert (
        before + timedelta(minutes=14, seconds=55)
        <= context.expires_at
        <= before
        + timedelta(
            minutes=15,
            seconds=5,
        )
    )


def test_decode_access_token_context_rejects_refresh_token_type() -> None:
    from core.exceptions import UnauthorizedError
    from domains.auth.security import create_refresh_token

    refresh_token, _jti, _family_id = create_refresh_token("00000000-0000-4000-8000-000000000001")

    with pytest.raises(UnauthorizedError, match="access token"):
        security.decode_access_token_context(refresh_token)


async def test_current_user_dependency_rejects_access_token_missing_jti() -> None:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "00000000-0000-4000-8000-000000000001",
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "type": "access",
        },
        get_settings().jwt_secret_key.get_secret_value(),
        algorithm=security.JWT_ALGORITHM,
    )

    application = FastAPI()

    @application.get("/protected")
    async def protected(_current_user: Any = Depends(security.get_current_user)) -> dict[str, str]:
        return {"status": "ok"}

    application.dependency_overrides[get_async_session] = ExplodingSession
    application.dependency_overrides[get_redis_dep] = CapturingRedis

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Access token is missing a valid jti claim."}
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_blacklist_jti_stores_ttl_until_token_expiration() -> None:
    redis = CapturingRedis()
    expires_at = datetime.now(UTC) + timedelta(seconds=123)

    await security.blacklist_jti(redis, "access-jti-ttl", expires_at=expires_at)  # type: ignore[arg-type]

    assert len(redis.set_calls) == 1
    call = redis.set_calls[0]
    assert call["key"] == "jwt:blacklist:access-jti-ttl"
    assert call["value"] == "1"
    assert 115 <= call["ex"] <= 123


async def test_is_jti_blacklisted_reads_stored_jti_existence() -> None:
    redis = CapturingRedis()

    assert (
        await security.is_jti_blacklisted(  # type: ignore[arg-type]
            redis,
            "stored-access-jti",
        )
        is False
    )

    await security.blacklist_jti(redis, "stored-access-jti", ttl_seconds=60)  # type: ignore[arg-type]

    assert (
        await security.is_jti_blacklisted(  # type: ignore[arg-type]
            redis,
            "stored-access-jti",
        )
        is True
    )
    assert (
        await security.is_jti_blacklisted(  # type: ignore[arg-type]
            redis,
            "other-access-jti",
        )
        is False
    )


async def test_logout_route_rejects_missing_bearer_token_before_service_resolution(
    logout_service: FakeLogoutService,
) -> None:
    service_factory_calls = 0

    def service_factory() -> FakeLogoutService:
        nonlocal service_factory_calls
        service_factory_calls += 1
        return logout_service

    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[_get_service] = service_factory
    application.dependency_overrides[get_async_session] = lambda: object()
    application.dependency_overrides[get_redis_dep] = CapturingRedis

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "refresh.jwt"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert service_factory_calls == 0
    assert logout_service.calls == []


async def test_logout_route_passes_current_access_token_jti_and_exp_to_service(
    app: FastAPI,
    logout_service: FakeLogoutService,
) -> None:
    token = create_access_token("00000000-0000-4000-8000-000000000001", jti="logout-jti")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "refresh.jwt"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204
    assert len(logout_service.calls) == 1
    call = logout_service.calls[0]
    assert call["refresh_token"] == "refresh.jwt"
    assert call["access_jti"] == "logout-jti"
    assert isinstance(call["access_expires_at"], datetime)
    assert call["access_expires_at"].tzinfo is not None
