"""Password-reset request schema validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from domains.auth.schemas import (
    PasswordResetRequest,
    PasswordResetRequestResponse,
)


def _error_by_location(exc: ValidationError) -> dict[str, dict[str, object]]:
    return {".".join(str(part) for part in error["loc"]): error for error in exc.errors()}


def test_password_reset_request_requires_email() -> None:
    with pytest.raises(ValidationError) as exc_info:
        PasswordResetRequest.model_validate({})

    errors = _error_by_location(exc_info.value)

    assert errors["email"]["type"] == "missing"


@pytest.mark.parametrize("email", ["not-an-email", "alice@", "@example.com"])
def test_password_reset_request_rejects_invalid_email_formats(email: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        PasswordResetRequest.model_validate({"email": email})

    errors = _error_by_location(exc_info.value)

    assert errors["email"]["type"] == "value_error"


def test_password_reset_request_normalizes_email_whitespace_and_case() -> None:
    request = PasswordResetRequest.model_validate({"email": "  ALICE@EXAMPLE.COM  "})

    assert request.email == "alice@example.com"


def test_password_reset_request_response_is_generic() -> None:
    response = PasswordResetRequestResponse()

    assert response.model_dump() == {
        "message": "If an account with that email exists, a reset link has been sent."
    }
