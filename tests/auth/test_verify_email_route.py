"""Email verification HTTP route tests."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from domains.auth.router import _get_service, router
from domains.auth.service import AuthService

_EMAIL = "verify-route@example.com"
_PASSWORD = "Password1!"


def _build_app(service: AuthService) -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[_get_service] = lambda: service
    return application


async def test_verify_email_endpoint_marks_user_verified(
    fake_repo: Any,
    fake_redis: Any,
) -> None:
    service = AuthService(repo=fake_repo, redis=fake_redis)
    app = _build_app(service)
    user, raw_token = await service.signup(_EMAIL, _PASSWORD, "Verifier")
    verification = await fake_repo.get_email_verification_by_token(raw_token)

    assert user.is_verified is False
    assert verification is not None
    assert verification.used is False

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(f"/api/v1/auth/verify-email/{raw_token}")

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Email verified successfully."
    assert body["user"]["email"] == _EMAIL
    assert body["user"]["is_verified"] is True
    assert user.is_verified is True
    assert verification.used is True
