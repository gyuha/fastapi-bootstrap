"""Auth domain repository — all database I/O lives here.

The repository pattern keeps SQL queries out of the service layer.
All methods are ``async`` and accept an :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

Usage::

    from {{ cookiecutter.package_name }}.domains.auth.repository import AuthRepository

    repo = AuthRepository(session)
    user = await repo.get_user_by_email("alice@example.com")
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from {{ cookiecutter.package_name }}.domains.auth.models import (
    EmailVerification,
    OAuthAccount,
    PasswordReset,
    RefreshToken,
    Role,
    User,
)
from {{ cookiecutter.package_name }}.domains.auth.security import hash_token


class AuthRepository:
    """Thin data-access layer for the auth domain."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── User ─────────────────────────────────────────────────────────────────

    async def get_user_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles).selectinload(Role.permissions))
        )
        return result.scalar_one_or_none()

    async def get_user_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            select(User)
            .where(User.email == email.lower())
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
            email=email.lower(),
            hashed_password=hashed_password,
            display_name=display_name,
        )
        self._session.add(user)
        await self._session.flush()  # get the generated id before commit
        return user

    async def mark_user_verified(self, user_id: uuid.UUID) -> None:
        await self._session.execute(
            update(User).where(User.id == user_id).values(is_verified=True)
        )

    async def update_user_password(self, user_id: uuid.UUID, hashed_password: str) -> None:
        await self._session.execute(
            update(User).where(User.id == user_id).values(hashed_password=hashed_password)
        )

    # ── Roles ─────────────────────────────────────────────────────────────────

    async def get_role_by_name(self, name: str) -> Role | None:
        result = await self._session.execute(
            select(Role).where(Role.name == name)
        )
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
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.jti == jti)
        )
        return result.scalar_one_or_none()

    async def get_refresh_token_by_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def revoke_refresh_token(self, jti: str) -> None:
        await self._session.execute(
            update(RefreshToken).where(RefreshToken.jti == jti).values(revoked=True)
        )

    async def revoke_all_user_refresh_tokens(self, user_id: uuid.UUID) -> None:
        """Revoke all active refresh tokens for a user (family revocation on reuse)."""
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked == False)  # noqa: E712
            .values(revoked=True)
        )

    async def delete_refresh_token(self, jti: str) -> None:
        await self._session.execute(
            delete(RefreshToken).where(RefreshToken.jti == jti)
        )

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
        )
        self._session.add(ev)
        await self._session.flush()
        return ev

    async def get_email_verification_by_token(self, raw_token: str) -> EmailVerification | None:
        result = await self._session.execute(
            select(EmailVerification)
            .where(EmailVerification.token_hash == hash_token(raw_token))
        )
        return result.scalar_one_or_none()

    async def mark_email_verification_used(self, ev_id: uuid.UUID) -> None:
        await self._session.execute(
            update(EmailVerification).where(EmailVerification.id == ev_id).values(used=True)
        )

    # ── PasswordReset ─────────────────────────────────────────────────────────

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
        )
        self._session.add(pr)
        await self._session.flush()
        return pr

    async def get_password_reset_by_token(self, raw_token: str) -> PasswordReset | None:
        result = await self._session.execute(
            select(PasswordReset)
            .where(PasswordReset.token_hash == hash_token(raw_token))
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
            select(OAuthAccount)
            .where(
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
