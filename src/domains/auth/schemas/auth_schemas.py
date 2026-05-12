"""Auth domain Pydantic schemas.

Request / Response models for all auth endpoints.

Naming convention:
  * ``<Entity>Create``  — request body for creation
  * ``<Entity>Response`` — response body (never includes hashed_password)
  * ``<Entity>Request`` — generic request body that doesn't fit create/update
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

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
    display_name: str = Field(min_length=1, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: object) -> object:
        """Trim and lower-case email before EmailStr format validation."""
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        """Require a production-grade password shape for signup payloads."""
        if any(c.isspace() for c in v):
            raise ValueError("Password must not contain whitespace.")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter.")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        if not re.search(r"[^A-Za-z0-9\s]", v):
            raise ValueError("Password must contain at least one special character.")
        return v

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, v: object) -> object:
        """Trim display_name and reject blank user-facing names."""
        if isinstance(v, str):
            display_name = v.strip()
            if not display_name:
                raise ValueError("Display name is required.")
            return display_name
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
    password: str = Field(max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: object) -> object:
        """Trim and lower-case the user identifier before EmailStr validation."""
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("password", mode="before")
    @classmethod
    def reject_blank_password(cls, v: object) -> object:
        """Reject blank login passwords before the request reaches the service layer."""
        if isinstance(v, str) and not v.strip():
            raise ValueError("Password is required.")
        return v


class TokenResponse(BaseModel):
    """JWT pair returned by login and refresh endpoints."""

    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - JWT token type constant, not a password.
    expires_in: int = Field(gt=0, description="Access token TTL in seconds.")


class RefreshRequest(BaseModel):
    """Request body for POST /auth/refresh."""

    refresh_token: str = Field(min_length=1)

    @field_validator("refresh_token", mode="before")
    @classmethod
    def reject_blank_refresh_token(cls, v: object) -> object:
        """Reject blank refresh tokens before service-layer rotation logic runs."""
        if isinstance(v, str) and not v.strip():
            raise ValueError("Refresh token is required.")
        return v


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

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: object) -> object:
        """Trim and lower-case email before EmailStr format validation."""
        if isinstance(v, str):
            return v.strip().lower()
        return v


class PasswordResetRequestResponse(BaseModel):
    """Response body for POST /auth/password-reset.

    The message is intentionally generic to prevent account enumeration.
    """

    message: str = "If an account with that email exists, a reset link has been sent."


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


class OAuthLoginURLResponse(BaseModel):
    """Response body for GET /auth/oauth/{provider}/login."""

    authorization_url: str
    state: str


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
