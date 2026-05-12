"""Signup request/response schema validation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from domains.auth.schemas import (
    SignupRequest,
    SignupResponse,
    UserResponse,
)


def _error_by_location(exc: ValidationError) -> dict[str, dict[str, object]]:
    return {".".join(str(part) for part in error["loc"]): error for error in exc.errors()}


def test_signup_request_requires_email_password_and_display_name() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SignupRequest.model_validate({})

    errors = _error_by_location(exc_info.value)

    assert errors["email"]["type"] == "missing"
    assert errors["password"]["type"] == "missing"
    assert errors["display_name"]["type"] == "missing"


@pytest.mark.parametrize("email", ["not-an-email", "alice@", "@example.com"])
def test_signup_request_rejects_invalid_email_formats(email: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        SignupRequest.model_validate(
            {
                "email": email,
                "password": "Password1!",
                "display_name": "Alice",
            }
        )

    errors = _error_by_location(exc_info.value)

    assert errors["email"]["type"] == "value_error"


def test_signup_request_normalizes_email_and_display_name_whitespace() -> None:
    request = SignupRequest.model_validate(
        {
            "email": "  ALICE@EXAMPLE.COM  ",
            "password": "Password1!",
            "display_name": "  Alice Kim  ",
        }
    )

    assert request.email == "alice@example.com"
    assert request.display_name == "Alice Kim"


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short1!", "at least 8 characters"),
        ("password1!", "uppercase letter"),
        ("PASSWORD1!", "lowercase letter"),
        ("Password!", "digit"),
        ("Password1", "special character"),
        ("Password 1!", "must not contain whitespace"),
    ],
)
def test_signup_request_rejects_weak_passwords(password: str, message: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        SignupRequest.model_validate(
            {
                "email": "alice@example.com",
                "password": password,
                "display_name": "Alice",
            }
        )

    errors = _error_by_location(exc_info.value)

    assert message in str(errors["password"]["msg"])


@pytest.mark.parametrize("display_name", ["", "   "])
def test_signup_request_rejects_blank_display_name(display_name: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        SignupRequest.model_validate(
            {
                "email": "alice@example.com",
                "password": "Password1!",
                "display_name": display_name,
            }
        )

    errors = _error_by_location(exc_info.value)

    assert "Display name is required" in str(errors["display_name"]["msg"])


def test_signup_response_serializes_public_user_and_message_only() -> None:
    user = UserResponse(
        id=uuid4(),
        email="alice@example.com",
        display_name="Alice",
        is_verified=False,
        is_active=True,
        created_at=datetime.now(UTC),
    )

    response = SignupResponse(user=user)
    payload = response.model_dump()

    assert payload["message"] == "Verification email sent."
    assert payload["user"]["email"] == "alice@example.com"
    assert "hashed_password" not in payload["user"]
