"""Auth domain email helpers.

Sends transactional emails via ``fastapi-mail``.

In development (APP_ENV=development), mail is routed to Mailpit
(configured in docker-compose.yml) via the SMTP settings in .env.example.

In production, set MAIL_SERVER / MAIL_USERNAME / MAIL_PASSWORD / MAIL_FROM
environment variables to point to a real SMTP relay. Password-reset emails use
FRONTEND_RESET_CONFIRM_URL_BASE as the frontend route base and append the token.

Usage::

    from domains.auth.email import send_verification_email

    await send_verification_email(user_email="alice@example.com", token="abc123")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import structlog
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from core.config import get_settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class VerificationEmailPayload:
    """Complete verification-email payload prepared before SMTP dispatch."""

    recipient: str
    subject: str
    body: str
    verification_url: str
    token: str


@dataclass(frozen=True, slots=True)
class PasswordResetEmailPayload:
    """Complete password-reset payload prepared before SMTP dispatch."""

    recipient: str
    subject: str
    body: str
    reset_url: str
    token: str


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """Rendered email subject and body."""

    subject: str
    body: str


class AuthEmailSender(Protocol):
    """Application mail-service port used by the auth domain.

    Keeping the port explicit makes signup email dispatch testable without
    patching module globals and keeps FastAPI/SMTP details out of
    :class:`~app.domains.auth.service.AuthService`.
    """

    async def send_verification_email(self, user_email: str, token: str) -> None:
        """Send an email-verification link to *user_email*."""

    async def send_password_reset_email(self, user_email: str, token: str) -> None:
        """Send a password-reset link to *user_email*."""


def build_verification_email_payload(user_email: str, token: str) -> VerificationEmailPayload:
    """Build the complete email-verification payload for *user_email*."""
    s = get_settings()
    frontend_url = s.frontend_url.rstrip("/")
    verification_url = f"{frontend_url}/auth/verify-email/{token}"
    body = (
        "Hello,\n\n"
        "Please verify your email address by clicking the link below:\n\n"
        f"  {verification_url}\n\n"
        "The link expires in 24 hours.\n\n"
        "If you did not register, ignore this email.\n"
    )
    return VerificationEmailPayload(
        recipient=user_email,
        subject="Verify your email address",
        body=body,
        verification_url=verification_url,
        token=token,
    )


def render_password_reset_email(reset_confirm_url: str) -> RenderedEmail:
    """Render password-reset email copy for an already-built confirm URL."""
    body = (
        "Hello,\n\n"
        "You requested a password reset. Click the link below:\n\n"
        f"  {reset_confirm_url}\n\n"
        "The link expires in 1 hour.\n\n"
        "If you did not request a reset, ignore this email.\n"
    )
    return RenderedEmail(subject="Reset your password", body=body)


def build_password_reset_email_payload(user_email: str, token: str) -> PasswordResetEmailPayload:
    """Build the complete password-reset payload with a URL-encoded token path segment."""
    s = get_settings()
    encoded_token = quote(token, safe="")
    reset_url = f"{s.frontend_reset_confirm_url_base}/{encoded_token}"
    rendered = render_password_reset_email(reset_confirm_url=reset_url)
    return PasswordResetEmailPayload(
        recipient=user_email,
        subject=rendered.subject,
        body=rendered.body,
        reset_url=reset_url,
        token=token,
    )


class FastAPIAuthEmailService:
    """Auth transactional email service backed by ``fastapi-mail``.

    The service reads SMTP settings from :func:`get_settings`, so local
    development uses Mailpit (``localhost:1025``) and
    production uses the SMTP environment variables rendered by the template.
    """

    def _get_mail_config(self) -> ConnectionConfig:
        s = get_settings()
        return ConnectionConfig(**s.mail_connection_config)

    async def send_mail(self, to: str, subject: str, body: str) -> None:
        """Send a plain-text email via fastapi-mail."""
        try:
            config = self._get_mail_config()
            fm = FastMail(config)
            message = MessageSchema(
                subject=subject,
                recipients=[to],
                body=body,
                subtype=MessageType.plain,
            )
            await fm.send_message(message)
            logger.info("email_sent", to=to, subject=subject)
        except Exception as exc:
            logger.error("email_send_failed", to=to, error=str(exc))
            raise

    async def send_verification_email(self, user_email: str, token: str) -> None:
        """Send the email-verification link to *user_email*."""
        payload = build_verification_email_payload(user_email, token)
        await self.send_mail(payload.recipient, payload.subject, payload.body)

    async def send_password_reset_email(self, user_email: str, token: str) -> None:
        """Send the password-reset link to *user_email*."""
        payload = build_password_reset_email_payload(user_email, token)
        await self.send_mail(payload.recipient, payload.subject, payload.body)


def get_auth_email_service() -> AuthEmailSender:
    """FastAPI dependency/factory for the auth application mail service."""
    return FastAPIAuthEmailService()


async def _send(to: str, subject: str, body: str) -> None:
    """Backward-compatible helper for direct email tests/custom code."""
    await FastAPIAuthEmailService().send_mail(to, subject, body)


async def send_verification_email(user_email: str, token: str) -> None:
    """Backward-compatible wrapper around the application mail service."""
    await FastAPIAuthEmailService().send_verification_email(user_email, token)


async def send_password_reset_email(user_email: str, token: str) -> None:
    """Backward-compatible wrapper around the application mail service."""
    await FastAPIAuthEmailService().send_password_reset_email(user_email, token)
