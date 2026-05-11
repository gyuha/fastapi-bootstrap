"""Auth domain email helpers.

Sends transactional emails via ``fastapi-mail``.

In development (APP_ENV=development), mail is routed to Mailpit
(configured in docker-compose.yml) via the SMTP settings in .env.example.

In production, set MAIL_SERVER / MAIL_USERNAME / MAIL_PASSWORD / MAIL_FROM
environment variables to point to a real SMTP relay.

Usage::

    from {{ cookiecutter.package_name }}.domains.auth.email import send_verification_email

    await send_verification_email(user_email="alice@example.com", token="abc123")
"""

from __future__ import annotations

import structlog
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from {{ cookiecutter.package_name }}.core.config import get_settings

logger = structlog.get_logger(__name__)


def _get_mail_config() -> ConnectionConfig:
    s = get_settings()
    return ConnectionConfig(**s.mail_connection_config)


async def _send(to: str, subject: str, body: str) -> None:
    """Send a plain-text email via fastapi-mail."""
    try:
        config = _get_mail_config()
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


async def send_verification_email(user_email: str, token: str) -> None:
    """Send the email-verification link to *user_email*."""
    s = get_settings()
    verify_url = f"{s.frontend_url}/auth/verify-email/{token}"
    body = (
        f"Hello,\n\n"
        f"Please verify your email address by clicking the link below:\n\n"
        f"  {verify_url}\n\n"
        f"The link expires in 24 hours.\n\n"
        f"If you did not register, ignore this email.\n"
    )
    await _send(user_email, "Verify your email address", body)


async def send_password_reset_email(user_email: str, token: str) -> None:
    """Send the password-reset link to *user_email*."""
    s = get_settings()
    reset_url = f"{s.frontend_url}/auth/reset-password/{token}"
    body = (
        f"Hello,\n\n"
        f"You requested a password reset. Click the link below:\n\n"
        f"  {reset_url}\n\n"
        f"The link expires in 1 hour.\n\n"
        f"If you did not request a reset, ignore this email.\n"
    )
    await _send(user_email, "Reset your password", body)
