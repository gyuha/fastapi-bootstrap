"""Refresh-token request and response schema validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from domains.auth.schemas import RefreshRequest, TokenResponse


def _error_by_location(exc: ValidationError) -> dict[str, dict[str, object]]:
    return {".".join(str(part) for part in error["loc"]): error for error in exc.errors()}


def test_refresh_request_requires_refresh_token_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        RefreshRequest.model_validate({})

    errors = _error_by_location(exc_info.value)

    assert errors["refresh_token"]["type"] == "missing"


@pytest.mark.parametrize("refresh_token", ["", "   "])
def test_refresh_request_rejects_blank_refresh_token(refresh_token: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        RefreshRequest.model_validate({"refresh_token": refresh_token})

    errors = _error_by_location(exc_info.value)

    assert "Refresh token is required" in str(errors["refresh_token"]["msg"])


def test_refresh_request_preserves_non_blank_refresh_token() -> None:
    request = RefreshRequest.model_validate({"refresh_token": "refresh.jwt"})

    assert request.refresh_token == "refresh.jwt"


def test_token_response_contract_returns_bearer_jwt_pair_with_access_ttl_seconds() -> None:
    response = TokenResponse.model_validate(
        {
            "access_token": "access.jwt",
            "refresh_token": "refresh.jwt",
            "token_type": "bearer",
            "expires_in": 900,
        }
    )

    assert response.access_token == "access.jwt"
    assert response.refresh_token == "refresh.jwt"
    assert response.token_type == "bearer"
    assert response.expires_in == 900


@pytest.mark.parametrize(
    "payload",
    [
        {
            "access_token": "access.jwt",
            "refresh_token": "refresh.jwt",
            "token_type": "Cookie",
            "expires_in": 900,
        },
        {
            "access_token": "access.jwt",
            "refresh_token": "refresh.jwt",
            "token_type": "Bearer",
            "expires_in": 0,
        },
    ],
)
def test_token_response_rejects_non_bearer_transport_or_non_positive_ttl(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TokenResponse.model_validate(payload)
