"""Auth domain service — business logic for all auth operations.

The service layer orchestrates repository calls, security operations, and
email delivery.  It has no knowledge of HTTP; errors are raised as
:class:`~{{ cookiecutter.package_name }}.core.exceptions.AppError` subclasses which the router
converts to HTTP responses.

Usage::

    repo = AuthRepository(session)
    svc = AuthService(repo, redis)

    user, tokens = await svc.signup("alice@example.com", "password123")
    tokens = await svc.login("alice@example.com", "password123")
    tokens = await svc.refresh(refresh_token_str)
    await svc.logout(refresh_token_str, access_jti)
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from {{ cookiecutter.package_name }}.core.exceptions import (
    ConflictError,
    NotFoundError,
    UnauthorizedError,
)
from {{ cookiecutter.package_name }}.domains.auth.email import (
    send_password_reset_email,
    send_verification_email,
)
from {{ cookiecutter.package_name }}.domains.auth.models import User
from {{ cookiecutter.package_name }}.domains.auth.repository import AuthRepository
from {{ cookiecutter.package_name }}.domains.auth.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    blacklist_jti,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)

logger = structlog.get_logger(__name__)

# Verification token TTL
EMAIL_VERIFY_EXPIRE_HOURS: int = 24
PASSWORD_RESET_EXPIRE_HOURS: int = 1


class AuthService:
    """Business logic for the auth domain.

    Parameters
    ----------
    repo:
        :class:`AuthRepository` instance scoped to the current request's DB session.
    redis:
        Shared Redis client for JWT blacklisting and OAuth state.
    """

    def __init__(self, repo: AuthRepository, redis: Redis) -> None:  # type: ignore[type-arg]
        self._repo = repo
        self._redis = redis

    # ── Signup ────────────────────────────────────────────────────────────────

    async def signup(
        self,
        email: str,
        password: str,
        display_name: str | None = None,
    ) -> tuple["User", str]:
        """Register a new user.

        Returns
        -------
        tuple[User, str]
            The created :class:`User` and the raw email-verification token
            (must be emailed to the user by the caller).

        Raises
        ------
        ConflictError
            If a user with *email* already exists.
        """
        existing = await self._repo.get_user_by_email(email)
        if existing is not None:
            raise ConflictError(f"An account with email '{email}' already exists.")

        hashed = hash_password(password)
        user = await self._repo.create_user(email, hashed, display_name)

        # Issue verification token
        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=EMAIL_VERIFY_EXPIRE_HOURS)
        await self._repo.create_email_verification(user.id, raw_token, expires_at)

        # Assign default "user" role if it exists
        default_role = await self._repo.get_role_by_name("user")
        if default_role:
            await self._repo.assign_role_to_user(user, default_role)

        logger.info("user_created", user_id=str(user.id), email=email)
        return user, raw_token

    async def signup_and_send_email(
        self,
        email: str,
        password: str,
        display_name: str | None = None,
    ) -> "User":
        """Register a new user and send the verification email."""
        user, raw_token = await self.signup(email, password, display_name)
        try:
            await send_verification_email(email, raw_token)
        except Exception as exc:
            logger.error("verification_email_failed", user_id=str(user.id), error=str(exc))
            # Don't fail signup if email delivery fails — user can request resend
        return user

    # ── Email verification ────────────────────────────────────────────────────

    async def verify_email(self, raw_token: str) -> "User":
        """Mark a user's email as verified.

        Raises
        ------
        UnauthorizedError
            If the token is invalid, expired, or already used.
        NotFoundError
            If the associated user no longer exists.
        """
        ev = await self._repo.get_email_verification_by_token(raw_token)
        if ev is None:
            raise UnauthorizedError("Invalid verification token.")
        if ev.used:
            raise UnauthorizedError("Verification token already used.")
        if ev.expires_at < datetime.now(UTC):
            raise UnauthorizedError("Verification token has expired.")

        await self._repo.mark_email_verification_used(ev.id)
        await self._repo.mark_user_verified(ev.user_id)

        user = await self._repo.get_user_by_id(ev.user_id)
        if user is None:
            raise NotFoundError("User")

        logger.info("email_verified", user_id=str(user.id))
        return user

    # ── Login ─────────────────────────────────────────────────────────────────

    async def login(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate a user and issue a JWT pair.

        Returns
        -------
        dict
            ``access_token``, ``refresh_token``, ``token_type``, ``expires_in``.

        Raises
        ------
        UnauthorizedError
            If credentials are invalid or the user is not active.
        """
        user = await self._repo.get_user_by_email(email)
        if user is None or not user.hashed_password:
            raise UnauthorizedError("Invalid email or password.")
        if not verify_password(password, user.hashed_password):
            raise UnauthorizedError("Invalid email or password.")
        if not user.is_active:
            raise UnauthorizedError("Account is deactivated.")

        return await self._issue_tokens(user)

    # ── Refresh ───────────────────────────────────────────────────────────────

    async def refresh(self, refresh_token_str: str) -> dict[str, Any]:
        """Rotate a refresh token and issue a new JWT pair.

        Reuse detection: if the incoming token is already revoked, all tokens
        in the same family are revoked (session family invalidation).

        Raises
        ------
        UnauthorizedError
            If the token is invalid, expired, revoked, or from an unknown user.
        """
        try:
            payload = decode_token(refresh_token_str)
        except UnauthorizedError:
            raise UnauthorizedError("Invalid or expired refresh token.")

        if payload.get("type") != "refresh":
            raise UnauthorizedError("Token is not a refresh token.")

        jti: str = payload.get("jti", "")
        family_id: str = payload.get("fid", str(uuid.uuid4()))
        raw: str = payload.get("raw", "")
        user_id_str: str = payload.get("sub", "")

        token_row = await self._repo.get_refresh_token_by_jti(jti)
        if token_row is None:
            # Token unknown — potential reuse of an already-rotated token
            logger.warning("refresh_token_reuse_detected", jti=jti)
            # Revoke the entire family by user_id
            try:
                user_id = uuid.UUID(user_id_str)
                await self._repo.revoke_all_user_refresh_tokens(user_id)
            except ValueError:
                pass
            raise UnauthorizedError("Refresh token reuse detected. All sessions revoked.")

        if token_row.revoked:
            # Token already rotated — reuse attack
            logger.warning(
                "refresh_token_reuse_detected",
                jti=jti,
                user_id=str(token_row.user_id),
            )
            await self._repo.revoke_all_user_refresh_tokens(token_row.user_id)
            raise UnauthorizedError("Refresh token reuse detected. All sessions revoked.")

        if token_row.expires_at < datetime.now(UTC):
            raise UnauthorizedError("Refresh token has expired.")

        # Validate token_hash matches
        if token_row.token_hash != hash_token(raw):
            raise UnauthorizedError("Refresh token tampered.")

        # Revoke the current token (rotation)
        await self._repo.revoke_refresh_token(jti)

        user = await self._repo.get_user_by_id(token_row.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("User not found or inactive.")

        # Issue new tokens in the same family
        return await self._issue_tokens(user, family_id=token_row.family_id)

    # ── Logout ────────────────────────────────────────────────────────────────

    async def logout(self, refresh_token_str: str, access_jti: str | None = None) -> None:
        """Revoke a refresh token and optionally blacklist the access token.

        Parameters
        ----------
        refresh_token_str:
            The raw refresh token JWT string from the client.
        access_jti:
            The ``jti`` of the current access token to add to Redis blacklist.
        """
        try:
            payload = decode_token(refresh_token_str)
            jti: str = payload.get("jti", "")
            token_row = await self._repo.get_refresh_token_by_jti(jti)
            if token_row:
                await self._repo.revoke_refresh_token(jti)
        except UnauthorizedError:
            pass  # Token already expired — logout is still valid

        if access_jti:
            await blacklist_jti(self._redis, access_jti)

        logger.info("user_logged_out", access_jti=access_jti)

    # ── Password reset ────────────────────────────────────────────────────────

    async def request_password_reset(self, email: str) -> None:
        """Send a password-reset link to *email*.

        Does NOT raise if the email is not found (prevents user enumeration).
        """
        user = await self._repo.get_user_by_email(email)
        if user is None:
            logger.info("password_reset_unknown_email", email=email)
            return  # silent no-op

        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=PASSWORD_RESET_EXPIRE_HOURS)
        await self._repo.create_password_reset(user.id, raw_token, expires_at)

        try:
            await send_password_reset_email(email, raw_token)
        except Exception as exc:
            logger.error("password_reset_email_failed", email=email, error=str(exc))

    async def confirm_password_reset(self, raw_token: str, new_password: str) -> None:
        """Apply a password-reset token.

        Raises
        ------
        UnauthorizedError
            If the token is invalid, expired, or already used.
        """
        pr = await self._repo.get_password_reset_by_token(raw_token)
        if pr is None:
            raise UnauthorizedError("Invalid password-reset token.")
        if pr.used:
            raise UnauthorizedError("Password-reset token already used.")
        if pr.expires_at < datetime.now(UTC):
            raise UnauthorizedError("Password-reset token has expired.")

        await self._repo.mark_password_reset_used(pr.id)
        hashed = hash_password(new_password)
        await self._repo.update_user_password(pr.user_id, hashed)

        # Revoke all sessions for security
        await self._repo.revoke_all_user_refresh_tokens(pr.user_id)

        logger.info("password_reset_completed", user_id=str(pr.user_id))

    # ── OAuth provisioning ────────────────────────────────────────────────────

    async def oauth_provision_user(
        self,
        provider: str,
        provider_user_id: str,
        email: str,
        display_name: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> tuple["User", dict[str, Any]]:
        """Find or create a user from an OAuth callback.

        Looks up an existing :class:`OAuthAccount` first; if not found, creates
        or links a :class:`User` (matching by email), then creates the account row.

        Returns
        -------
        tuple[User, dict]
            The (possibly new) user and their JWT pair.
        """
        oa = await self._repo.get_oauth_account(provider, provider_user_id)
        if oa is not None:
            # Existing OAuth account — update tokens, issue JWT
            await self._repo.update_oauth_account(oa.id, access_token, refresh_token, expires_at)
            user = await self._repo.get_user_by_id(oa.user_id)
            if user is None:
                raise UnauthorizedError("Associated user not found.")
        else:
            # Check if a user with this email already exists
            user = await self._repo.get_user_by_email(email)
            if user is None:
                user = await self._repo.create_user(email, None, display_name)
                await self._repo.mark_user_verified(user.id)
                # Assign default role
                default_role = await self._repo.get_role_by_name("user")
                if default_role:
                    await self._repo.assign_role_to_user(user, default_role)

            await self._repo.create_oauth_account(
                user.id, provider, provider_user_id, access_token, refresh_token, expires_at
            )

        logger.info(
            "oauth_user_provisioned",
            provider=provider,
            user_id=str(user.id),
        )
        tokens = await self._issue_tokens(user)
        return user, tokens

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _issue_tokens(
        self,
        user: "User",
        family_id: str | None = None,
    ) -> dict[str, Any]:
        """Create access + refresh token pair and persist the refresh token."""
        # Create access token
        access_jti = str(uuid.uuid4())
        access_token = create_access_token(user.id, jti=access_jti)

        # Create refresh token
        refresh_token_str, refresh_jti, _family_id = create_refresh_token(
            user.id, family_id=family_id
        )

        # Extract the raw random payload for storage
        refresh_payload = decode_token(refresh_token_str)
        raw_refresh = refresh_payload.get("raw", "")

        expires_at = datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        await self._repo.create_refresh_token(
            user_id=user.id,
            jti=refresh_jti,
            raw_token=raw_refresh,
            family_id=_family_id,
            expires_at=expires_at,
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token_str,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
