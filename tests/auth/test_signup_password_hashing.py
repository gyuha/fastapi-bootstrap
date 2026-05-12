"""Signup password hashing tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from domains.auth.models import User
from domains.auth.security import verify_password
from domains.auth.service import AuthService


@dataclass
class FakeAuthRepository:
    """Repository fake that records exactly what signup persists."""

    persisted_email: str | None = None
    persisted_password: str | None = None
    persisted_display_name: str | None = None
    verification_tokens: list[str] = field(default_factory=list)

    async def get_user_by_email(self, email: str) -> User | None:
        return None

    async def create_user(
        self,
        email: str,
        hashed_password: str | None = None,
        display_name: str | None = None,
    ) -> User:
        self.persisted_email = email
        self.persisted_password = hashed_password
        self.persisted_display_name = display_name
        return User(
            id=uuid.uuid4(),
            email=email,
            hashed_password=hashed_password,
            display_name=display_name,
            is_verified=False,
            is_active=True,
        )

    async def create_email_verification(
        self,
        user_id: uuid.UUID,
        raw_token: str,
        expires_at: datetime,
    ) -> Any:
        self.verification_tokens.append(raw_token)
        return None

    async def get_role_by_name(self, name: str) -> None:
        return None


async def test_signup_hashes_password_with_argon2_before_persistence() -> None:
    repo = FakeAuthRepository()
    service = AuthService(repo, redis=None)  # type: ignore[arg-type]
    raw_password = "Password1!"

    await service.signup(
        email="alice@example.com",
        password=raw_password,
        display_name="Alice",
    )

    assert repo.persisted_password is not None
    assert repo.persisted_password != raw_password
    assert repo.persisted_password.startswith("$argon2")
    assert raw_password not in repo.persisted_password
    assert verify_password(raw_password, repo.persisted_password) is True
