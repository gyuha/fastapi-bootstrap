"""Password-reset HTTP route tests."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from domains.auth.router import _get_service, router
from domains.auth.service import AuthService

_PASSWORD = "Password1!"
_GENERIC_MESSAGE = "If an account with that email exists, a reset link has been sent."


class CapturingMailService:
    """Auth email-service fake that records outbound messages."""

    def __init__(self) -> None:
        self.verification_emails: list[tuple[str, str]] = []
        self.password_reset_emails: list[tuple[str, str]] = []

    async def send_verification_email(self, user_email: str, token: str) -> None:
        self.verification_emails.append((user_email, token))

    async def send_password_reset_email(self, user_email: str, token: str) -> None:
        self.password_reset_emails.append((user_email, token))


def _build_app(service: AuthService) -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[_get_service] = lambda: service
    return application


async def test_password_reset_endpoint_returns_same_response_for_existing_and_unknown_email(
    fake_repo: Any,
    fake_redis: Any,
) -> None:
    mail_service = CapturingMailService()
    service = AuthService(repo=fake_repo, redis=fake_redis, mail_service=mail_service)
    await service.signup("alice@example.com", _PASSWORD, "Alice")
    app = _build_app(service)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        existing_response = await client.post(
            "/api/v1/auth/password-reset",
            json={"email": "  ALICE@EXAMPLE.COM  "},
        )
        unknown_response = await client.post(
            "/api/v1/auth/password-reset",
            json={"email": "missing@example.com"},
        )

    assert existing_response.status_code == 202
    assert unknown_response.status_code == 202
    assert existing_response.json() == unknown_response.json() == {"message": _GENERIC_MESSAGE}
    assert len(fake_repo.password_resets) == 1
    assert len(mail_service.password_reset_emails) == 1
    assert mail_service.password_reset_emails[0][0] == "alice@example.com"


async def test_password_reset_endpoint_rejects_invalid_email_before_service_call(
    fake_repo: Any,
    fake_redis: Any,
) -> None:
    mail_service = CapturingMailService()
    service = AuthService(repo=fake_repo, redis=fake_redis, mail_service=mail_service)
    app = _build_app(service)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/password-reset",
            json={"email": "not-an-email"},
        )

    assert response.status_code == 422
    assert fake_repo.password_resets == {}
    assert mail_service.password_reset_emails == []
