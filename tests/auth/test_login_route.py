"""Login HTTP route validation tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.exceptions import UnauthorizedError
from domains.auth.router import _get_service, router
from domains.auth.security import decode_token
from domains.auth.service import AuthService


class FakeLoginService:
    """Minimal auth application-service fake for route-level login validation tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.invalid_credentials: set[tuple[str, str]] = set()

    async def login(self, email: str, password: str) -> dict[str, Any]:
        self.calls.append({"email": email, "password": password})
        if (email, password) in self.invalid_credentials:
            raise UnauthorizedError("Invalid email or password.")
        return {
            "access_token": "access.jwt",
            "refresh_token": "refresh.jwt",
            "token_type": "bearer",
            "expires_in": 900,
        }


@pytest.fixture
def login_service() -> FakeLoginService:
    return FakeLoginService()


@pytest.fixture
def app(login_service: FakeLoginService) -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[_get_service] = lambda: login_service
    return application


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "not-an-email", "password": "Password1!"},
        {"email": "alice@example.com", "password": ""},
        {"email": "alice@example.com", "password": "   "},
    ],
)
async def test_login_rejects_malformed_payload_before_service_call(
    app: FastAPI,
    login_service: FakeLoginService,
    payload: dict[str, Any],
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/auth/login", json=payload)

    assert response.status_code == 422
    assert login_service.calls == []


async def test_login_returns_success_token_response_for_valid_credentials(
    app: FastAPI,
    login_service: FakeLoginService,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "  ALICE@EXAMPLE.COM  ", "password": "Password1!"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "access.jwt",
        "refresh_token": "refresh.jwt",
        "token_type": "bearer",
        "expires_in": 900,
    }
    assert login_service.calls == [{"email": "alice@example.com", "password": "Password1!"}]


async def test_login_route_returns_bearer_jwt_pair_for_verified_user(
    auth_service: AuthService,
    fake_repo: Any,
) -> None:
    user, verification_token = await auth_service.signup(
        "alice@example.com",
        "Password1!",
        "Alice",
    )
    await auth_service.verify_email(verification_token)

    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[_get_service] = lambda: auth_service

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "Password1!"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str)
    assert isinstance(body["refresh_token"], str)
    assert len(body["access_token"].split(".")) == 3
    assert len(body["refresh_token"].split(".")) == 3

    access_payload = decode_token(body["access_token"])
    refresh_payload = decode_token(body["refresh_token"])
    assert access_payload["type"] == "access"
    assert refresh_payload["type"] == "refresh"
    assert access_payload["sub"] == str(user.id)
    assert refresh_payload["sub"] == str(user.id)
    assert await fake_repo.get_refresh_token_by_jti(refresh_payload["jti"]) is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "missing@example.com", "password": "Password1!"},
        {"email": "alice@example.com", "password": "WrongPassword1!"},
    ],
)
async def test_login_returns_authentication_error_for_unknown_user_or_wrong_password(
    app: FastAPI,
    login_service: FakeLoginService,
    payload: dict[str, str],
) -> None:
    login_service.invalid_credentials.add((payload["email"], payload["password"]))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/v1/auth/login", json=payload)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password."}
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert login_service.calls == [{"email": payload["email"], "password": payload["password"]}]
