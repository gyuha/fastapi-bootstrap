"""Signup HTTP route tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.exceptions import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from domains.auth.router import _get_service, router


class FakeSignupService:
    """Minimal auth application-service fake for route-level signup tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def signup_and_send_email(
        self,
        email: str,
        password: str,
        display_name: str,
    ) -> Any:
        self.calls.append(
            {
                "email": email,
                "password": password,
                "display_name": display_name,
            }
        )
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            id=uuid.uuid4(),
            email=email,
            display_name=display_name,
            is_verified=False,
            is_active=True,
            created_at=datetime.now(UTC),
        )


@pytest.fixture
def signup_service() -> FakeSignupService:
    return FakeSignupService()


@pytest.fixture
def app(signup_service: FakeSignupService) -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[_get_service] = lambda: signup_service
    return application


async def test_signup_delegates_validated_payload_to_auth_service(
    app: FastAPI,
    signup_service: FakeSignupService,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/signup",
            json={
                "email": "  ALICE@EXAMPLE.COM  ",
                "password": "Password1!",
                "display_name": "  Alice Kim  ",
            },
        )

    assert response.status_code == 201
    assert signup_service.calls == [
        {
            "email": "alice@example.com",
            "password": "Password1!",
            "display_name": "Alice Kim",
        }
    ]
    body = response.json()
    assert body["message"] == "Verification email sent. Please check your inbox."
    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["display_name"] == "Alice Kim"
    assert body["user"]["is_verified"] is False
    assert "hashed_password" not in body["user"]


async def test_signup_rejects_invalid_payload_before_service_call(
    app: FastAPI,
    signup_service: FakeSignupService,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/signup",
            json={
                "email": "alice@example.com",
                "password": "weak",
                "display_name": "Alice",
            },
        )

    assert response.status_code == 422
    assert signup_service.calls == []


@pytest.mark.parametrize(
    ("service_error", "expected_status", "expected_detail"),
    [
        (
            ConflictError("An account with email 'alice@example.com' already exists."),
            409,
            "An account with email 'alice@example.com' already exists.",
        ),
        (
            AppError("Signup payload rejected by application service."),
            400,
            "Signup payload rejected by application service.",
        ),
        (
            UnauthorizedError("Email verification is required before this action."),
            401,
            "Email verification is required before this action.",
        ),
        (
            ForbiddenError("Signup is disabled for this tenant."),
            403,
            "Signup is disabled for this tenant.",
        ),
        (NotFoundError("Invite"), 404, "Invite not found."),
    ],
)
async def test_signup_maps_application_service_errors_to_http_responses(
    app: FastAPI,
    signup_service: FakeSignupService,
    service_error: AppError,
    expected_status: int,
    expected_detail: str,
) -> None:
    signup_service.error = service_error

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/signup",
            json={
                "email": "alice@example.com",
                "password": "Password1!",
                "display_name": "Alice",
            },
        )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
