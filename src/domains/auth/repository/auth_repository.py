"""Auth domain repository — all database I/O lives here.

The repository pattern keeps SQL queries out of the service layer.
All methods are ``async`` and accept an :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

Usage::

    from domains.auth.repository import AuthRepository

    repo = AuthRepository(session)
    user = await repo.get_user_by_email("alice@example.com")
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from domains.auth.models import (
    EmailVerification,
    OAuthAccount,
    PasswordReset,
    RefreshToken,
    Role,
    User,
)
from domains.auth.security import hash_token


def normalize_email(email: str) -> str:
    """Return the canonical email form used for auth identity and uniqueness."""
    return email.strip().lower()


class AuthRepository:
    """Thin data-access layer for the auth domain."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run repository operations in a single database transaction."""
        if self._session.in_transaction():
            async with self._session.begin_nested():
                yield
        else:
            async with self._session.begin():
                yield

    # ── User ─────────────────────────────────────────────────────────────────

    async def get_user_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles).selectinload(Role.permissions))
        )
        return result.scalar_one_or_none()

    async def get_user_by_email(self, email: str) -> User | None:
        normalized_email = normalize_email(email)
        result = await self._session.execute(
            select(User)
            .where(User.email == normalized_email)
            .options(selectinload(User.roles).selectinload(Role.permissions))
        )
        return result.scalar_one_or_none()

    async def create_user(
        self,
        email: str,
        hashed_password: str | None = None,
        display_name: str | None = None,
    ) -> User:
        user = User(
            email=normalize_email(email),
            hashed_password=hashed_password,
            display_name=display_name.strip() if display_name is not None else None,
            is_verified=False,
            is_active=True,
        )
        self._session.add(user)
        await self._session.flush()  # get the generated id before commit
        return user

    async def mark_user_verified(self, user_id: uuid.UUID) -> None:
        await self._session.execute(update(User).where(User.id == user_id).values(is_verified=True))

    async def update_user_password(self, user_id: uuid.UUID, hashed_password: str) -> None:
        await self._session.execute(
            update(User).where(User.id == user_id).values(hashed_password=hashed_password)
        )

    # ── Roles ─────────────────────────────────────────────────────────────────

    async def get_role_by_name(self, name: str) -> Role | None:
        result = await self._session.execute(select(Role).where(Role.name == name))
        return result.scalar_one_or_none()

    async def assign_role_to_user(self, user: User, role: Role) -> None:
        if role not in user.roles:
            user.roles.append(role)
            await self._session.flush()

    # ── RefreshToken ─────────────────────────────────────────────────────────

    async def create_refresh_token(
        self,
        user_id: uuid.UUID,
        jti: str,
        raw_token: str,
        family_id: str,
        expires_at: datetime,
    ) -> RefreshToken:
        token = RefreshToken(
            user_id=user_id,
            jti=jti,
            token_hash=hash_token(raw_token),
            family_id=family_id,
            expires_at=expires_at,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_refresh_token_by_jti(self, jti: str) -> RefreshToken | None:
        result = await self._session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        return result.scalar_one_or_none()

    async def get_refresh_token_by_jti_for_update(self, jti: str) -> RefreshToken | None:
        """Return a refresh token row while holding a row-level write lock."""
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.jti == jti).with_for_update(of=RefreshToken)
        )
        return result.scalar_one_or_none()

    async def get_refresh_token_by_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def mark_refresh_token_rotated(
        self,
        jti: str,
        replaced_by_jti: str,
        rotated_at: datetime | None = None,
    ) -> None:
        """Mark a refresh token as rotated in one status+metadata update."""
        rotation_time = rotated_at or datetime.now(UTC)
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti)
            .values(
                revoked=True,
                revoked_at=rotation_time,
                rotated_at=rotation_time,
                replaced_by_jti=replaced_by_jti,
            )
        )

    async def revoke_refresh_token(self, jti: str, revoked_at: datetime | None = None) -> None:
        revoke_time = revoked_at or datetime.now(UTC)
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti)
            .values(revoked=True, revoked_at=revoke_time)
        )

    async def revoke_all_user_refresh_tokens(self, user_id: uuid.UUID) -> None:
        """Revoke all active refresh tokens for a user."""
        revoke_time = datetime.now(UTC)
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked == False)  # noqa: E712
            .values(revoked=True, revoked_at=revoke_time)
        )

    async def invalidate_all_user_sessions(self, user_id: uuid.UUID) -> None:
        """Invalidate all active auth sessions for a user after token reuse.

        In this lightweight template, each active refresh-token row is the
        server-side session record. Keeping this as a separate repository method
        gives the reuse-detection path an explicit session-invalidation boundary
        if a dedicated sessions table is introduced later.
        """
        await self.revoke_all_user_refresh_tokens(user_id)

    async def delete_refresh_token(self, jti: str) -> None:
        await self._session.execute(delete(RefreshToken).where(RefreshToken.jti == jti))

    # ── EmailVerification ─────────────────────────────────────────────────────

    async def create_email_verification(
        self,
        user_id: uuid.UUID,
        raw_token: str,
        expires_at: datetime,
    ) -> EmailVerification:
        ev = EmailVerification(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
            used=False,
            created_at=datetime.now(UTC),
        )
        self._session.add(ev)
        await self._session.flush()
        return ev

    async def get_email_verification_by_token(self, raw_token: str) -> EmailVerification | None:
        result = await self._session.execute(
            select(EmailVerification).where(EmailVerification.token_hash == hash_token(raw_token))
        )
        return result.scalar_one_or_none()

    async def mark_email_verification_used(self, ev_id: uuid.UUID) -> None:
        await self._session.execute(
            update(EmailVerification).where(EmailVerification.id == ev_id).values(used=True)
        )

    # ── PasswordReset ─────────────────────────────────────────────────────────

    async def mark_user_password_resets_used(self, user_id: uuid.UUID) -> None:
        await self._session.execute(
            update(PasswordReset)
            .where(PasswordReset.user_id == user_id, PasswordReset.used.is_(False))
            .values(used=True)
        )

    async def create_password_reset(
        self,
        user_id: uuid.UUID,
        raw_token: str,
        expires_at: datetime,
    ) -> PasswordReset:
        pr = PasswordReset(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
            used=False,
            created_at=datetime.now(UTC),
        )
        self._session.add(pr)
        await self._session.flush()
        return pr

    async def get_password_reset_by_token(self, raw_token: str) -> PasswordReset | None:
        result = await self._session.execute(
            select(PasswordReset).where(PasswordReset.token_hash == hash_token(raw_token))
        )
        return result.scalar_one_or_none()

    async def mark_password_reset_used(self, pr_id: uuid.UUID) -> None:
        await self._session.execute(
            update(PasswordReset).where(PasswordReset.id == pr_id).values(used=True)
        )

    # ── OAuthAccount ─────────────────────────────────────────────────────────

    async def get_oauth_account(
        self,
        provider: str,
        provider_user_id: str,
    ) -> OAuthAccount | None:
        result = await self._session.execute(
            select(OAuthAccount).where(
                OAuthAccount.provider == provider,
                OAuthAccount.provider_user_id == provider_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def create_oauth_account(
        self,
        user_id: uuid.UUID,
        provider: str,
        provider_user_id: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> OAuthAccount:
        oa = OAuthAccount(
            user_id=user_id,
            provider=provider,
            provider_user_id=provider_user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )
        self._session.add(oa)
        await self._session.flush()
        return oa

    async def update_oauth_account(
        self,
        oa_id: uuid.UUID,
        access_token: str | None,
        refresh_token: str | None,
        expires_at: datetime | None,
    ) -> None:
        await self._session.execute(
            update(OAuthAccount)
            .where(OAuthAccount.id == oa_id)
            .values(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
            )
        )
