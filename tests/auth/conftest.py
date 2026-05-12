"""Auth test suite conftest.

Provides lightweight fixtures for testing auth business logic without
hitting a real database or Redis.  All I/O is faked with in-memory stubs.

Fixtures
--------
* :func:`fake_redis` — async Redis stub with get/set/exists/delete/ping.
* :func:`fake_session` — async SQLAlchemy session stub (commit/rollback/flush noop).
* :func:`fake_repo` — in-memory :class:`AuthRepository`-compatible stub.
* :func:`auth_service` — :class:`AuthService` wired to fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake Redis
# ---------------------------------------------------------------------------


class FakeRedis:
    """Minimal async Redis stub for auth tests."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self.expirations: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self._store[key] = value
        self.expirations[key] = ex

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self.expirations.pop(key, None)

    async def ping(self) -> bool:
        return True

    def clear(self) -> None:
        self._store.clear()
        self.expirations.clear()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


# ---------------------------------------------------------------------------
# Fake AuthRepository
# ---------------------------------------------------------------------------


class FakeAuthRepository:
    """In-memory stub for :class:`AuthRepository`.

    Stores users, refresh tokens, email verifications, and password resets
    in simple dicts keyed by their primary attributes.
    """

    def __init__(self) -> None:
        self.users: dict[str, Any] = {}  # email → user-like object
        self.users_by_id: dict[str, Any] = {}  # str(id) → user-like object
        self.refresh_tokens: dict[str, Any] = {}  # jti → token row
        self.email_verifications: dict[str, Any] = {}  # token_hash → ev row
        self.password_resets: dict[str, Any] = {}  # token_hash → pr row
        self.oauth_accounts: dict[str, Any] = {}  # (provider, uid) → oa row
        self.roles: dict[str, Any] = {}  # name → role
        self.transaction_entries = 0
        self.transaction_exits = 0
        self.locked_refresh_jtis: list[str] = []
        self.invalidated_session_user_ids: list[str] = []

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transaction_entries += 1
        try:
            yield
        finally:
            self.transaction_exits += 1

    async def get_user_by_email(self, email: str) -> Any | None:
        return self.users.get(email.strip().lower())

    async def get_user_by_id(self, user_id: Any) -> Any | None:
        return self.users_by_id.get(str(user_id))

    async def create_user(
        self,
        email: str,
        hashed_password: str | None = None,
        display_name: str | None = None,
    ) -> Any:
        import uuid

        user = MagicMock()
        user.id = uuid.uuid4()
        normalized_email = email.strip().lower()
        user.email = normalized_email
        user.hashed_password = hashed_password
        user.display_name = display_name.strip() if display_name is not None else None
        user.is_verified = False
        user.is_active = True
        user.created_at = datetime.now(UTC)
        user.roles = []
        self.users[normalized_email] = user
        self.users_by_id[str(user.id)] = user
        return user

    async def mark_user_verified(self, user_id: Any) -> None:
        user = self.users_by_id.get(str(user_id))
        if user:
            user.is_verified = True

    async def update_user_password(self, user_id: Any, hashed_password: str) -> None:
        user = self.users_by_id.get(str(user_id))
        if user:
            user.hashed_password = hashed_password

    async def get_role_by_name(self, name: str) -> Any | None:
        return self.roles.get(name)

    async def assign_role_to_user(self, user: Any, role: Any) -> None:
        if role not in user.roles:
            user.roles.append(role)

    async def create_refresh_token(
        self,
        user_id: Any,
        jti: str,
        raw_token: str,
        family_id: str,
        expires_at: datetime,
    ) -> Any:
        from domains.auth.security import hash_token

        row = MagicMock()
        row.id = jti  # use jti as id for simplicity
        row.user_id = user_id
        row.jti = jti
        row.token_hash = hash_token(raw_token)
        row.family_id = family_id
        row.revoked = False
        row.revoked_at = None
        row.rotated_at = None
        row.replaced_by_jti = None
        row.expires_at = expires_at
        self.refresh_tokens[jti] = row
        return row

    async def get_refresh_token_by_jti(self, jti: str) -> Any | None:
        return self.refresh_tokens.get(jti)

    async def get_refresh_token_by_jti_for_update(self, jti: str) -> Any | None:
        self.locked_refresh_jtis.append(jti)
        return self.refresh_tokens.get(jti)

    async def get_refresh_token_by_hash(self, token_hash: str) -> Any | None:
        for row in self.refresh_tokens.values():
            if row.token_hash == token_hash:
                return row
        return None

    async def mark_refresh_token_rotated(
        self,
        jti: str,
        replaced_by_jti: str,
        rotated_at: datetime | None = None,
    ) -> None:
        row = self.refresh_tokens.get(jti)
        if row:
            rotation_time = rotated_at or datetime.now(UTC)
            row.revoked = True
            row.revoked_at = rotation_time
            row.rotated_at = rotation_time
            row.replaced_by_jti = replaced_by_jti

    async def revoke_refresh_token(self, jti: str, revoked_at: datetime | None = None) -> None:
        row = self.refresh_tokens.get(jti)
        if row:
            row.revoked = True
            row.revoked_at = revoked_at or datetime.now(UTC)

    async def revoke_all_user_refresh_tokens(self, user_id: Any) -> None:
        revoke_time = datetime.now(UTC)
        for row in self.refresh_tokens.values():
            if str(row.user_id) == str(user_id):
                row.revoked = True
                row.revoked_at = revoke_time

    async def invalidate_all_user_sessions(self, user_id: Any) -> None:
        self.invalidated_session_user_ids.append(str(user_id))
        await self.revoke_all_user_refresh_tokens(user_id)

    async def delete_refresh_token(self, jti: str) -> None:
        self.refresh_tokens.pop(jti, None)

    async def create_email_verification(
        self,
        user_id: Any,
        raw_token: str,
        expires_at: datetime,
    ) -> Any:
        import uuid

        from domains.auth.security import hash_token

        row = MagicMock()
        row.id = uuid.uuid4()
        row.user_id = user_id
        row.token_hash = hash_token(raw_token)
        row.expires_at = expires_at
        row.used = False
        row.created_at = datetime.now(UTC)
        self.email_verifications[row.token_hash] = row
        return row

    async def get_email_verification_by_token(self, raw_token: str) -> Any | None:
        from domains.auth.security import hash_token

        h = hash_token(raw_token)
        return self.email_verifications.get(h)

    async def mark_email_verification_used(self, ev_id: Any) -> None:
        for row in self.email_verifications.values():
            if str(row.id) == str(ev_id):
                row.used = True

    async def mark_user_password_resets_used(self, user_id: Any) -> None:
        for row in self.password_resets.values():
            if str(row.user_id) == str(user_id) and row.used is False:
                row.used = True

    async def create_password_reset(
        self,
        user_id: Any,
        raw_token: str,
        expires_at: datetime,
    ) -> Any:
        import uuid

        from domains.auth.security import hash_token

        row = MagicMock()
        row.id = uuid.uuid4()
        row.user_id = user_id
        row.token_hash = hash_token(raw_token)
        row.expires_at = expires_at
        row.used = False
        self.password_resets[row.token_hash] = row
        return row

    async def get_password_reset_by_token(self, raw_token: str) -> Any | None:
        from domains.auth.security import hash_token

        h = hash_token(raw_token)
        return self.password_resets.get(h)

    async def mark_password_reset_used(self, pr_id: Any) -> None:
        for row in self.password_resets.values():
            if str(row.id) == str(pr_id):
                row.used = True

    async def get_oauth_account(self, provider: str, provider_user_id: str) -> Any | None:
        return self.oauth_accounts.get((provider, provider_user_id))

    async def create_oauth_account(
        self,
        user_id: Any,
        provider: str,
        provider_user_id: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> Any:
        import uuid

        row = MagicMock()
        row.id = uuid.uuid4()
        row.user_id = user_id
        row.provider = provider
        row.provider_user_id = provider_user_id
        row.access_token = access_token
        row.refresh_token = refresh_token
        row.expires_at = expires_at
        self.oauth_accounts[(provider, provider_user_id)] = row
        return row

    async def update_oauth_account(
        self,
        oa_id: Any,
        access_token: str | None,
        refresh_token: str | None,
        expires_at: datetime | None,
    ) -> None:
        for row in self.oauth_accounts.values():
            if str(row.id) == str(oa_id):
                row.access_token = access_token
                row.refresh_token = refresh_token
                row.expires_at = expires_at


@pytest.fixture
def fake_repo() -> FakeAuthRepository:
    return FakeAuthRepository()


# ---------------------------------------------------------------------------
# AuthService fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_service(fake_repo: FakeAuthRepository, fake_redis: FakeRedis) -> Any:
    """Return an :class:`AuthService` wired to in-memory fakes."""
    from domains.auth.service import AuthService

    return AuthService(repo=fake_repo, redis=fake_redis)
