"""Auth email backend tests.

These tests pin the local development email topology:

* docker-compose runs Mailpit and exposes SMTP on localhost:1025.
* FastAPI runs on the host via uvicorn/uv.
* signup verification emails are delivered through fastapi-mail using those env settings.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from core.config import get_settings
from domains.auth import email as email_module


class CapturingFastMail:
    """Test double that captures FastMail config and outgoing messages."""

    instances: ClassVar[list[CapturingFastMail]] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.messages: list[Any] = []
        self.__class__.instances.append(self)

    async def send_message(self, message: Any) -> None:
        self.messages.append(message)


def test_password_reset_email_template_renders_subject_and_body_from_injected_link() -> None:
    """Password-reset copy is rendered from a reset-confirm link supplied by the caller."""
    reset_confirm_url = "https://app.example.com/reset-confirm/reset-token-123"

    rendered = email_module.render_password_reset_email(reset_confirm_url=reset_confirm_url)

    assert rendered.subject == "Reset your password"
    assert rendered.body.startswith("Hello,")
    assert reset_confirm_url in rendered.body
    assert "{reset_confirm_url}" not in rendered.body
    assert "The link expires in 1 hour." in rendered.body
    assert "If you did not request a reset, ignore this email." in rendered.body


def test_build_verification_email_payload_includes_recipient_subject_body_and_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verification emails expose a complete payload before SMTP dispatch."""
    verification_value = "verify-token-123"
    with patch.dict(
        os.environ,
        {
            "SECRET_KEY": "test-secret-key",
            "JWT_SECRET_KEY": "test-jwt-secret-key",
            "FRONTEND_URL": "https://app.example.com/",
        },
        clear=True,
    ):
        get_settings.cache_clear()
        try:
            payload = email_module.build_verification_email_payload(
                "alice@example.com",
                verification_value,
            )
        finally:
            get_settings.cache_clear()

    assert payload.recipient == "alice@example.com"
    assert payload.subject == "Verify your email address"
    assert payload.token == verification_value
    assert payload.verification_url == (
        "https://app.example.com/auth/verify-email/verify-token-123"
    )
    assert payload.body.startswith("Hello,")
    assert "Please verify your email address" in payload.body
    assert payload.verification_url in payload.body
    assert verification_value in payload.body
    assert "The link expires in 24 hours." in payload.body


@pytest.mark.asyncio
async def test_verification_email_uses_mailpit_dev_smtp_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """send_verification_email routes signup mail to local Mailpit SMTP."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(email_module, "FastMail", CapturingFastMail)
    CapturingFastMail.instances.clear()

    with patch.dict(
        os.environ,
        {
            "SECRET_KEY": "test-secret-key",
            "JWT_SECRET_KEY": "test-jwt-secret-key",
            "FRONTEND_URL": "http://localhost:3000",
            "MAIL_SERVER": "localhost",
            "MAIL_PORT": "1025",
            "MAIL_USERNAME": "",
            "MAIL_PASSWORD": "",
            "MAIL_FROM": "noreply@fastapi-bootstrap.example.com",
            "MAIL_FROM_NAME": "Example API",
            "MAIL_STARTTLS": "false",
            "MAIL_SSL_TLS": "false",
        },
        clear=True,
    ):
        get_settings.cache_clear()
        try:
            await email_module.send_verification_email("alice@example.com", "verify-token-123")
        finally:
            get_settings.cache_clear()

    assert len(CapturingFastMail.instances) == 1
    mailer = CapturingFastMail.instances[0]
    assert mailer.config.MAIL_SERVER == "localhost"
    assert mailer.config.MAIL_PORT == 1025
    assert mailer.config.MAIL_STARTTLS is False
    assert mailer.config.MAIL_SSL_TLS is False
    assert mailer.config.USE_CREDENTIALS is False
    assert mailer.config.VALIDATE_CERTS is False

    assert len(mailer.messages) == 1
    message = mailer.messages[0]
    assert message.subject == "Verify your email address"
    assert len(message.recipients) == 1
    assert "alice@example.com" in str(message.recipients[0])
    assert "http://localhost:3000/auth/verify-email/verify-token-123" in message.body


@pytest.mark.asyncio
async def test_password_reset_email_uses_reset_confirm_url_base(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Password-reset mail links to FRONTEND_RESET_CONFIRM_URL_BASE plus the encoded token."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(email_module, "FastMail", CapturingFastMail)
    CapturingFastMail.instances.clear()
    reset_value = "reset token/with+symbols?email=alice@example.com"
    encoded_value = "reset%20token%2Fwith%2Bsymbols%3Femail%3Dalice%40example.com"

    with patch.dict(
        os.environ,
        {
            "SECRET_KEY": "test-secret-key",
            "JWT_SECRET_KEY": "test-jwt-secret-key",
            "FRONTEND_URL": "https://app.example.com",
            "FRONTEND_RESET_CONFIRM_URL_BASE": "https://app.example.com/reset-confirm/",
            "MAIL_SERVER": "localhost",
            "MAIL_PORT": "1025",
            "MAIL_USERNAME": "",
            "MAIL_PASSWORD": "",
            "MAIL_FROM": "noreply@fastapi-bootstrap.example.com",
            "MAIL_FROM_NAME": "Example API",
            "MAIL_STARTTLS": "false",
            "MAIL_SSL_TLS": "false",
        },
        clear=True,
    ):
        get_settings.cache_clear()
        try:
            await email_module.send_password_reset_email(
                "alice@example.com",
                reset_value,
            )
        finally:
            get_settings.cache_clear()

    assert len(CapturingFastMail.instances) == 1
    message = CapturingFastMail.instances[0].messages[0]
    assert message.subject == "Reset your password"
    assert f"https://app.example.com/reset-confirm/{encoded_value}" in message.body
    assert f"https://app.example.com/reset-confirm/{reset_value}" not in message.body
    assert "https://app.example.com/auth/reset-password/" not in message.body
