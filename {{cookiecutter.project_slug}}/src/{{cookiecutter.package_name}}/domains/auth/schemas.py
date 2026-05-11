"""Auth domain Pydantic schemas.

Request / Response models for all auth endpoints.

Naming convention:
  * ``<Entity>Create``  — request body for creation
  * ``<Entity>Response`` — response body (never includes hashed_password)
  * ``<Entity>Request`` — generic request body that doesn't fit create/update
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class UserResponse(BaseModel):
    """Public user representation — never includes hashed_password."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    is_verified: bool
    is_active: bool
    created_at: datetime


class SignupRequest(BaseModel):
    """Request body for POST /auth/signup."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        """Require at least one digit and one letter."""
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain at least one letter.")
        return v


class SignupResponse(BaseModel):
    """Response body for POST /auth/signup."""

    user: UserResponse
    message: str = "Verification email sent."


# ---------------------------------------------------------------------------
# Login / Tokens
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Request body for POST /auth/login."""

    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """JWT pair returned by login and refresh endpoints."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(description="Access token TTL in seconds.")


class RefreshRequest(BaseModel):
    """Request body for POST /auth/refresh."""

    refresh_token: str


class LogoutRequest(BaseModel):
    """Request body for POST /auth/logout."""

    refresh_token: str


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


class VerifyEmailResponse(BaseModel):
    """Response body for POST /auth/verify-email/{token}."""

    message: str = "Email verified successfully."
    user: UserResponse


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


class PasswordResetRequest(BaseModel):
    """Request body for POST /auth/password-reset (request reset link)."""

    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    """Request body for POST /auth/password-reset/confirm."""

    token: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain at least one letter.")
        return v


class PasswordResetConfirmResponse(BaseModel):
    """Response body for POST /auth/password-reset/confirm."""

    message: str = "Password reset successfully."


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

{% if cookiecutter.oauth_providers != "none" %}

class OAuthLoginURLResponse(BaseModel):
    """Response body for GET /auth/oauth/{provider}/login."""

    authorization_url: str
    state: str

{% endif %}

# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class RoleResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str | None


class PermissionResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    key: str
    description: str | None
