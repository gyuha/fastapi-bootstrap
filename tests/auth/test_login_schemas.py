"""Login request schema validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from domains.auth.schemas import LoginRequest


def _error_by_location(exc: ValidationError) -> dict[str, dict[str, object]]:
    return {".".join(str(part) for part in error["loc"]): error for error in exc.errors()}


def test_login_request_requires_user_identifier_and_password() -> None:
    with pytest.raises(ValidationError) as exc_info:
        LoginRequest.model_validate({})

    errors = _error_by_location(exc_info.value)

    assert errors["email"]["type"] == "missing"
    assert errors["password"]["type"] == "missing"


@pytest.mark.parametrize("email", ["not-an-email", "alice@", "@example.com"])
def test_login_request_rejects_invalid_user_identifier_formats(email: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        LoginRequest.model_validate({"email": email, "password": "Password1!"})

    errors = _error_by_location(exc_info.value)

    assert errors["email"]["type"] == "value_error"


def test_login_request_normalizes_email_identifier_whitespace() -> None:
    payload = {"email": "  ALICE@EXAMPLE.COM  ", "password": "Password1!"}
    request = LoginRequest.model_validate(payload)

    assert request.email == "alice@example.com"
    assert request.password == payload["password"]


@pytest.mark.parametrize("password", ["", "   "])
def test_login_request_rejects_blank_password(password: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        LoginRequest.model_validate({"email": "alice@example.com", "password": password})

    errors = _error_by_location(exc_info.value)

    assert "Password is required" in str(errors["password"]["msg"])
