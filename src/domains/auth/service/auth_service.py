"""Auth domain service — business logic for all auth operations.

The service layer orchestrates repository calls, security operations, and
email delivery.  It has no knowledge of HTTP; errors are raised as
:class:`~app.core.exceptions.AppError` subclasses which the router
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
from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    ConflictError,
    NotFoundError,
    UnauthorizedError,
)
from domains.auth.email import (
    AuthEmailSender,
    get_auth_email_service,
)
from domains.auth.models import RefreshToken, User
from domains.auth.repository import AuthRepository, normalize_email
from domains.auth.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
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


def _normalize_display_name(display_name: str | None, email: str) -> str:
    """Return a non-empty display name for a local signup identity."""
    if display_name is not None:
        normalized = display_name.strip()
        if normalized:
            return normalized

    local_part = normalize_email(email).split("@", maxsplit=1)[0]
    return local_part or "user"


def _refresh_token_reuse_state(token_row: RefreshToken | None) -> str:
    """Classify the persisted state that made a refresh token a reuse event."""
    if token_row is None:
        return "missing"
    if getattr(token_row, "rotated_at", None) is not None or getattr(
        token_row, "replaced_by_jti", None
    ):
        return "rotated"
    return "revoked"


def _log_refresh_token_reuse_detected(
    *,
    jti: str,
    user_id: str,
    token_state: str,
    family_id: str | None,
    replaced_by_jti: str | None,
) -> None:
    """Emit a structured security event for refresh-token reuse detection."""
    logger.warning(
        "refresh_token_reuse_detected",
        security_event_type="token_reuse",
        token_state=token_state,
        jti=jti,
        user_id=user_id,
        family_id=family_id,
        replaced_by_jti=replaced_by_jti,
    )


class AuthService:
    """Business logic for the auth domain.

    Parameters
    ----------
    repo:
        :class:`AuthRepository` instance scoped to the current request's DB session.
    redis:
        Shared Redis client for JWT blacklisting and OAuth state.
    mail_service:
        Application mail-service port for verification/password-reset emails.
    """

    def __init__(
        self,
        repo: AuthRepository,
        redis: Redis,
        mail_service: AuthEmailSender | None = None,
    ) -> None:
        self._repo = repo
        self._redis = redis
        self._mail_service = mail_service or get_auth_email_service()

    # ── Signup ────────────────────────────────────────────────────────────────

    async def signup(
        self,
        email: str,
        password: str,
        display_name: str | None = None,
    ) -> tuple[User, str]:
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
        normalized_email = normalize_email(email)
        normalized_display_name = _normalize_display_name(display_name, normalized_email)

        existing = await self._repo.get_user_by_email(normalized_email)
        if existing is not None:
            raise ConflictError(f"An account with email '{normalized_email}' already exists.")

        hashed = hash_password(password)
        try:
            user = await self._repo.create_user(normalized_email, hashed, normalized_display_name)
        except IntegrityError as exc:
            raise ConflictError(
                f"An account with email '{normalized_email}' already exists."
            ) from exc

        # Issue verification token
        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=EMAIL_VERIFY_EXPIRE_HOURS)
        await self._repo.create_email_verification(user.id, raw_token, expires_at)

        # Assign default "user" role if it exists
        default_role = await self._repo.get_role_by_name("user")
        if default_role:
            await self._repo.assign_role_to_user(user, default_role)

        logger.info("user_created", user_id=str(user.id), email=normalized_email)
        return user, raw_token

    async def signup_and_send_email(
        self,
        email: str,
        password: str,
        display_name: str | None = None,
    ) -> User:
        """Register a new user and send the verification email."""
        user, raw_token = await self.signup(email, password, display_name)
        try:
            await self._mail_service.send_verification_email(user.email, raw_token)
        except Exception as exc:
            logger.error("verification_email_failed", user_id=str(user.id), error=str(exc))
            # Don't fail signup if email delivery fails — user can request resend
        return user

    # ── Email verification ────────────────────────────────────────────────────

    async def verify_email(self, raw_token: str) -> User:
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

    async def authenticate_credentials(self, email: str, password: str) -> User | None:
        """Return the stored user for a valid email/password credential pair."""
        user = await self._repo.get_user_by_email(normalize_email(email))
        if user is None or not user.hashed_password:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user

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
        user = await self.authenticate_credentials(email, password)
        if user is None:
            raise UnauthorizedError("Invalid email or password.")
        if not user.is_active:
            raise UnauthorizedError("Account is deactivated.")
        if not user.is_verified:
            raise UnauthorizedError("Email verification is required before login.")

        return await self._issue_tokens(user)

    # ── Refresh ───────────────────────────────────────────────────────────────

    async def refresh(self, refresh_token_str: str) -> dict[str, Any]:
        """Rotate a refresh token and issue a new JWT pair.

        Reuse detection: if the incoming token is already revoked, all active
        refresh tokens and server-side session records for that user are invalidated.

        Raises
        ------
        UnauthorizedError
            If the token is invalid, expired, revoked, or from an unknown user.
        """
        try:
            payload = decode_token(refresh_token_str)
        except UnauthorizedError as exc:
            raise UnauthorizedError("Invalid or expired refresh token.") from exc

        if payload.get("type") != "refresh":
            raise UnauthorizedError("Token is not a refresh token.")

        jti: str = payload.get("jti", "")
        raw: str = payload.get("raw", "")
        user_id_str: str = payload.get("sub", "")
        try:
            user_id = uuid.UUID(user_id_str)
        except (AttributeError, TypeError, ValueError) as exc:
            raise UnauthorizedError("Invalid refresh token subject.") from exc

        async with self._repo.transaction():
            token_row = await self._repo.get_refresh_token_by_jti_for_update(jti)
            if token_row is None:
                # Token unknown — potential reuse of an already-rotated token
                _log_refresh_token_reuse_detected(
                    jti=jti,
                    user_id=str(user_id),
                    token_state=_refresh_token_reuse_state(token_row),
                    family_id=payload.get("fid"),
                    replaced_by_jti=None,
                )
                # Reuse is treated as account-level session compromise: revoke every
                # active refresh token/session record for this user, not just this family.
                await self._repo.invalidate_all_user_sessions(user_id)
                raise UnauthorizedError("Refresh token reuse detected. All sessions revoked.")

            if str(token_row.user_id) != str(user_id):
                logger.warning(
                    "refresh_token_subject_mismatch",
                    jti=jti,
                    token_user_id=str(token_row.user_id),
                    subject_user_id=str(user_id),
                )
                raise UnauthorizedError(
                    "Refresh token subject does not match persisted token user."
                )

            if token_row.revoked:
                # Token already revoked or rotated — reuse attack
                _log_refresh_token_reuse_detected(
                    jti=jti,
                    user_id=str(token_row.user_id),
                    token_state=_refresh_token_reuse_state(token_row),
                    family_id=token_row.family_id,
                    replaced_by_jti=token_row.replaced_by_jti,
                )
                await self._repo.invalidate_all_user_sessions(token_row.user_id)
                raise UnauthorizedError("Refresh token reuse detected. All sessions revoked.")

            if token_row.expires_at < datetime.now(UTC):
                raise UnauthorizedError("Refresh token has expired.")

            # Validate token_hash matches
            if token_row.token_hash != hash_token(raw):
                raise UnauthorizedError("Refresh token tampered.")

            user = await self._repo.get_user_by_id(token_row.user_id)
            if user is None or not user.is_active:
                raise UnauthorizedError("User not found or inactive.")

            # Issue the replacement token first, then update the locked session row with
            # both status and rotation metadata in the same transaction.  This avoids a
            # committed state where the old row is revoked but not linked to its successor.
            new_tokens = await self._issue_tokens(user, family_id=token_row.family_id)
            new_refresh_payload = decode_token(new_tokens["refresh_token"])
            await self._repo.mark_refresh_token_rotated(
                jti,
                replaced_by_jti=new_refresh_payload["jti"],
                rotated_at=datetime.now(UTC),
            )
            return new_tokens

    # ── Logout ────────────────────────────────────────────────────────────────

    async def logout(
        self,
        refresh_token_str: str,
        access_jti: str | None = None,
        access_expires_at: datetime | None = None,
    ) -> None:
        """Revoke a refresh token and optionally blacklist the access token.

        Parameters
        ----------
        refresh_token_str:
            The raw refresh token JWT string from the client.
        access_jti:
            The ``jti`` of the current access token to add to Redis blacklist.
        access_expires_at:
            The current access token's ``exp`` claim as an aware UTC datetime.
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
            await blacklist_jti(self._redis, access_jti, expires_at=access_expires_at)

        logger.info("user_logged_out", access_jti=access_jti)

    # ── Password reset ────────────────────────────────────────────────────────

    async def request_password_reset(self, email: str) -> None:
        """Send a password-reset link to *email*.

        Does NOT raise if the email is not found (prevents user enumeration).
        """
        normalized_email = normalize_email(email)
        user = await self._repo.get_user_by_email(normalized_email)
        if user is None:
            logger.info("password_reset_unknown_email", email=normalized_email)
            return  # silent no-op

        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=PASSWORD_RESET_EXPIRE_HOURS)
        await self._repo.mark_user_password_resets_used(user.id)
        await self._repo.create_password_reset(user.id, raw_token, expires_at)

        try:
            await self._mail_service.send_password_reset_email(user.email, raw_token)
        except Exception as exc:
            logger.error("password_reset_email_failed", email=user.email, error=str(exc))

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
    ) -> tuple[User, dict[str, Any]]:
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
        user: User,
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

        expires_at = datetime.fromtimestamp(refresh_payload["exp"], tz=UTC)
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
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
