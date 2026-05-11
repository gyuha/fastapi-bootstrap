"""Signup → Mailpit integration check.

This test is intentionally opt-in because it talks to a running host FastAPI
server and the Mailpit HTTP API. Run it after ``make dev`` has started the
local stack:

    RUN_MAILPIT_INTEGRATION=1 uv run pytest tests/auth/test_signup_mailpit_integration.py -v

It verifies the local developer loop promised by the template: a real signup
request to ``/api/v1/auth/signup`` produces a verification email visible in
Mailpit.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import anyio
import httpx
import pytest

pytestmark = pytest.mark.integration

_FASTAPI_BASE_URL = os.getenv(
    "FASTAPI_BASE_URL",
    "http://localhost:8000",
).rstrip("/")
_MAILPIT_BASE_URL = os.getenv(
    "MAILPIT_BASE_URL",
    "http://localhost:8025",
).rstrip("/")


def _mailpit_message_recipients(message: dict[str, Any]) -> list[str]:
    recipients: list[str] = []
    for recipient in message.get("To", []):
        address = recipient.get("Address") if isinstance(recipient, dict) else None
        if address:
            recipients.append(str(address).lower())
    return recipients


async def _wait_for_mailpit_message(
    client: httpx.AsyncClient,
    recipient: str,
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    normalized_recipient = recipient.lower()

    while time.monotonic() < deadline:
        response = await client.get(f"{_MAILPIT_BASE_URL}/api/v1/messages")
        response.raise_for_status()
        payload = response.json()
        messages = payload.get("messages", [])
        for message in messages:
            if normalized_recipient in _mailpit_message_recipients(message):
                return message
        await anyio.sleep(0.5)

    pytest.fail(f"No Mailpit email found for {recipient!r} within {timeout_seconds:.0f}s.")


@pytest.mark.skipif(
    os.getenv("RUN_MAILPIT_INTEGRATION") != "1",
    reason=(
        "Requires a running FastAPI server and Mailpit. Start `make dev`, then set "
        "RUN_MAILPIT_INTEGRATION=1."
    ),
)
async def test_signup_email_is_visible_in_mailpit() -> None:
    suffix = secrets.token_hex(6)
    email = f"mailpit_signup_{suffix}@example.com"
    password = f"MailpitT3st!{suffix}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        health = await client.get(f"{_FASTAPI_BASE_URL}/health")
        health.raise_for_status()

        mailpit = await client.get(f"{_MAILPIT_BASE_URL}/api/v1/messages")
        mailpit.raise_for_status()

        signup = await client.post(
            f"{_FASTAPI_BASE_URL}/api/v1/auth/signup",
            json={
                "email": email,
                "password": password,
                "display_name": "Mailpit Signup Tester",
            },
        )
        assert signup.status_code == 201, signup.text
        body = signup.json()
        assert body["user"]["email"] == email
        assert body["user"]["is_verified"] is False

        message = await _wait_for_mailpit_message(client, email)

    assert email in _mailpit_message_recipients(message)
    assert message.get("Subject") == "Verify your email address"
