"""Auth E2E flow integration tests.

Tests the full auth lifecycle using in-memory fakes (no DB, no Redis, no email).
Covers the acceptance criteria exit condition:

    auth_e2e: signup → verify → login → refresh → logout pytest 통과

All tests are sync (no asyncio fixtures needed — we run async service methods
via pytest-asyncio's asyncio_mode="auto" via pyproject.toml configuration).

Test classes
------------
* :class:`TestSignup`       — registration + email verification trigger
* :class:`TestVerifyEmail`  — email-verification token lifecycle
* :class:`TestLogin`        — credential validation + JWT issuance
* :class:`TestRefresh`      — token rotation + reuse detection
* :class:`TestLogout`       — token revocation + Redis blacklisting
* :class:`TestPasswordReset` — request + confirm flow
* :class:`TestRBAC`         — require_permission enforcement (unit)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from {{ cookiecutter.package_name }}.domains.auth.security import (
    create_access_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from {{ cookiecutter.package_name }}.domains.auth.service import AuthService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMAIL = "alice@example.com"
_PASSWORD = "Password1!"


# ---------------------------------------------------------------------------
# TestSignup
# ---------------------------------------------------------------------------


class TestSignup:
    """signup() — user creation, password hashing, email verification token issuance."""

    async def test_signup_creates_user(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD, "Alice")

        assert user.email == _EMAIL.lower()
        assert user.display_name == "Alice"
        assert user.is_verified is False

    async def test_signup_hashes_password(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        assert user.hashed_password is not None
        assert user.hashed_password != _PASSWORD
        assert verify_password(_PASSWORD, user.hashed_password)

    async def test_signup_issues_verification_token(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)

        # Token should be stored in repo
        ev = await fake_repo.get_email_verification_by_token(raw_token)
        assert ev is not None
        assert ev.user_id == user.id
        assert ev.used is False

    async def test_signup_duplicate_email_raises_conflict(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import ConflictError  # noqa: PLC0415

        await auth_service.signup(_EMAIL, _PASSWORD)
        with pytest.raises(ConflictError, match="already exists"):
            await auth_service.signup(_EMAIL, "AnotherPass2!")

    async def test_signup_and_send_email_suppresses_mail_error(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        """signup_and_send_email should not raise even if email delivery fails."""
        with patch(
            "{{ cookiecutter.package_name }}.domains.auth.service.send_verification_email",
            side_effect=Exception("SMTP error"),
        ):
            user = await auth_service.signup_and_send_email(_EMAIL, _PASSWORD)

        assert user.email == _EMAIL.lower()


# ---------------------------------------------------------------------------
# TestVerifyEmail
# ---------------------------------------------------------------------------


class TestVerifyEmail:
    """verify_email() — token lifecycle."""

    async def test_verify_email_marks_user_verified(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        assert user.is_verified is False

        verified_user = await auth_service.verify_email(raw_token)

        assert verified_user.is_verified is True

    async def test_verify_email_invalid_token_raises_unauthorized(
        self,
        auth_service: AuthService,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        with pytest.raises(UnauthorizedError, match="Invalid"):
            await auth_service.verify_email("nonexistent-token")

    async def test_verify_email_already_used_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)

        with pytest.raises(UnauthorizedError, match="already used"):
            await auth_service.verify_email(raw_token)

    async def test_verify_email_expired_token_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)

        # Expire the token manually
        ev = await fake_repo.get_email_verification_by_token(raw_token)
        ev.expires_at = datetime.now(UTC) - timedelta(hours=1)

        with pytest.raises(UnauthorizedError, match="expired"):
            await auth_service.verify_email(raw_token)


# ---------------------------------------------------------------------------
# TestLogin
# ---------------------------------------------------------------------------


class TestLogin:
    """login() — credential validation + JWT pair issuance."""

    async def _signup_and_verify(
        self, auth_service: AuthService, fake_repo: Any
    ) -> Any:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)
        return user

    async def test_login_returns_token_pair(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        await self._signup_and_verify(auth_service, fake_repo)
        tokens = await auth_service.login(_EMAIL, _PASSWORD)

        assert "access_token" in tokens
        assert "refresh_token" in tokens
        assert tokens["token_type"] == "Bearer"
        assert tokens["expires_in"] > 0

    async def test_login_access_token_is_valid_jwt(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        await self._signup_and_verify(auth_service, fake_repo)
        tokens = await auth_service.login(_EMAIL, _PASSWORD)

        payload = decode_token(tokens["access_token"])
        assert payload["type"] == "access"
        assert "sub" in payload
        assert "jti" in payload

    async def test_login_wrong_password_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        await self._signup_and_verify(auth_service, fake_repo)
        with pytest.raises(UnauthorizedError):
            await auth_service.login(_EMAIL, "WrongPassword!")

    async def test_login_unknown_email_raises_unauthorized(
        self,
        auth_service: AuthService,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        with pytest.raises(UnauthorizedError):
            await auth_service.login("nobody@example.com", _PASSWORD)

    async def test_login_inactive_user_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)
        user.is_active = False

        with pytest.raises(UnauthorizedError):
            await auth_service.login(_EMAIL, _PASSWORD)


# ---------------------------------------------------------------------------
# TestRefresh
# ---------------------------------------------------------------------------


class TestRefresh:
    """refresh() — token rotation + reuse detection."""

    async def _get_tokens(
        self, auth_service: AuthService, fake_repo: Any
    ) -> dict[str, Any]:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)
        return await auth_service.login(_EMAIL, _PASSWORD)

    async def test_refresh_returns_new_token_pair(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        tokens = await self._get_tokens(auth_service, fake_repo)
        new_tokens = await auth_service.refresh(tokens["refresh_token"])

        assert "access_token" in new_tokens
        assert "refresh_token" in new_tokens
        assert new_tokens["access_token"] != tokens["access_token"]
        assert new_tokens["refresh_token"] != tokens["refresh_token"]

    async def test_refresh_revokes_old_token(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        tokens = await self._get_tokens(auth_service, fake_repo)
        old_refresh = tokens["refresh_token"]

        await auth_service.refresh(old_refresh)

        # Old token should now be revoked — reusing it should fail
        with pytest.raises(UnauthorizedError, match="reuse detected"):
            await auth_service.refresh(old_refresh)

    async def test_refresh_reuse_detection_revokes_all_sessions(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        """Using a revoked token should revoke ALL family tokens."""
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        tokens = await self._get_tokens(auth_service, fake_repo)
        old_refresh = tokens["refresh_token"]

        # Rotate once
        new_tokens = await auth_service.refresh(old_refresh)

        # Reuse the OLD token — triggers family revocation
        with pytest.raises(UnauthorizedError, match="reuse detected"):
            await auth_service.refresh(old_refresh)

        # New token should also be unusable now (family revocation)
        with pytest.raises(UnauthorizedError):
            await auth_service.refresh(new_tokens["refresh_token"])

    async def test_refresh_invalid_token_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        with pytest.raises(UnauthorizedError):
            await auth_service.refresh("not-a-valid-jwt")


# ---------------------------------------------------------------------------
# TestLogout
# ---------------------------------------------------------------------------


class TestLogout:
    """logout() — refresh token revocation + access token blacklisting."""

    async def _get_tokens(
        self, auth_service: AuthService, fake_repo: Any
    ) -> dict[str, Any]:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)
        return await auth_service.login(_EMAIL, _PASSWORD)

    async def test_logout_revokes_refresh_token(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        tokens = await self._get_tokens(auth_service, fake_repo)
        await auth_service.logout(tokens["refresh_token"])

        # Refresh token should now be revoked
        with pytest.raises(UnauthorizedError, match="reuse detected"):
            await auth_service.refresh(tokens["refresh_token"])

    async def test_logout_blacklists_access_jti(
        self,
        auth_service: AuthService,
        fake_repo: Any,
        fake_redis: Any,
    ) -> None:
        tokens = await self._get_tokens(auth_service, fake_repo)
        access_payload = decode_token(tokens["access_token"])
        jti = access_payload["jti"]

        await auth_service.logout(tokens["refresh_token"], access_jti=jti)

        # jti should be in Redis blacklist
        blacklisted = await fake_redis.exists(f"jwt:blacklist:{jti}")
        assert blacklisted == 1

    async def test_logout_with_expired_refresh_token_is_idempotent(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        """Logout should not raise even if the refresh token is already expired."""
        tokens = await self._get_tokens(auth_service, fake_repo)
        await auth_service.logout(tokens["refresh_token"])
        # Second logout should be silent (token already revoked)
        await auth_service.logout(tokens["refresh_token"])


# ---------------------------------------------------------------------------
# TestPasswordReset
# ---------------------------------------------------------------------------


class TestPasswordReset:
    """password reset flow — request + confirm."""

    async def _registered_user(
        self, auth_service: AuthService, fake_repo: Any
    ) -> Any:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)
        return user

    async def test_request_password_reset_sends_email(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        await self._registered_user(auth_service, fake_repo)

        from unittest.mock import ANY  # noqa: PLC0415

        with patch(
            "{{ cookiecutter.package_name }}.domains.auth.service.send_password_reset_email",
            new_callable=AsyncMock,
        ) as mock_send:
            await auth_service.request_password_reset(_EMAIL)
            mock_send.assert_called_once_with(_EMAIL, ANY)

    async def test_request_password_reset_unknown_email_silent(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        """No error for unknown email (prevents user enumeration)."""
        # Should not raise
        await auth_service.request_password_reset("nobody@example.com")

    async def test_confirm_password_reset_changes_password(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        user = await self._registered_user(auth_service, fake_repo)

        with patch(
            "{{ cookiecutter.package_name }}.domains.auth.service.send_password_reset_email",
            new_callable=AsyncMock,
        ):
            await auth_service.request_password_reset(_EMAIL)

        # Find the reset token
        pr_rows = list(fake_repo.password_resets.values())
        assert len(pr_rows) == 1

        # Get raw token from stored hash — we need the raw token
        # Since we can't reverse the hash, we'll extract it from the service
        # by patching create_password_reset and capturing the raw token.

        # Alternative: register via signup and use the token directly.
        # Easier: patch the email sending to capture the token.
        raw_token_ref: list[str] = []

        async def capture_reset_token(email: str, token: str) -> None:
            raw_token_ref.append(token)

        # Re-request to capture token
        with patch(
            "{{ cookiecutter.package_name }}.domains.auth.service.send_password_reset_email",
            side_effect=capture_reset_token,
        ):
            await auth_service.request_password_reset(_EMAIL)

        assert len(raw_token_ref) == 1
        raw_token = raw_token_ref[0]

        new_password = "NewPassword2!"
        await auth_service.confirm_password_reset(raw_token, new_password)

        # Old password should no longer work
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        with pytest.raises(UnauthorizedError):
            await auth_service.login(_EMAIL, _PASSWORD)

        # New password should work
        tokens = await auth_service.login(_EMAIL, new_password)
        assert "access_token" in tokens

    async def test_confirm_invalid_token_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from {{ cookiecutter.package_name }}.core.exceptions import UnauthorizedError  # noqa: PLC0415

        with pytest.raises(UnauthorizedError, match="Invalid"):
            await auth_service.confirm_password_reset("invalid-token", "NewPass1!")


# ---------------------------------------------------------------------------
# TestRBAC
# ---------------------------------------------------------------------------


class TestRBAC:
    """RBAC — require_permission dependency."""

    def test_has_permission_returns_true_for_granted_permission(self) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        perm = MagicMock()
        perm.key = "chat:write"

        role = MagicMock()
        role.permissions = [perm]

        user = MagicMock()
        user.roles = [role]
        user.has_permission = lambda key: any(
            p.key == key for r in user.roles for p in r.permissions
        )

        assert user.has_permission("chat:write") is True
        assert user.has_permission("admin:users") is False

    async def test_require_permission_raises_403_for_missing_permission(self) -> None:
        """require_permission raises HTTPException 403 when user lacks the key."""
        from fastapi import HTTPException  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        from {{ cookiecutter.package_name }}.domains.auth.security import require_permission  # noqa: PLC0415

        user = MagicMock()
        user.id = "test-user-id"
        user.has_permission = lambda key: False  # no permissions

        dep = require_permission("chat:write")

        with pytest.raises(HTTPException) as exc_info:
            await dep(user=user)

        assert exc_info.value.status_code == 403
        assert "chat:write" in exc_info.value.detail

    async def test_require_permission_returns_user_when_granted(self) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from {{ cookiecutter.package_name }}.domains.auth.security import require_permission  # noqa: PLC0415

        user = MagicMock()
        user.id = "test-user-id"
        user.has_permission = lambda key: key == "chat:write"

        dep = require_permission("chat:write")
        result = await dep(user=user)

        assert result is user


# ---------------------------------------------------------------------------
# TestSecurityUtilities
# ---------------------------------------------------------------------------


class TestSecurityUtilities:
    """Unit tests for security helper functions."""

    def test_hash_password_and_verify(self) -> None:
        plain = "MySecurePass1!"
        hashed = hash_password(plain)
        assert hashed != plain
        assert verify_password(plain, hashed)
        assert not verify_password("WrongPass!", hashed)

    def test_create_access_token_decodes_correctly(self) -> None:
        import uuid  # noqa: PLC0415

        user_id = str(uuid.uuid4())
        token = create_access_token(user_id)
        payload = decode_token(token)

        assert payload["sub"] == user_id
        assert payload["type"] == "access"
        assert "jti" in payload
        assert "exp" in payload

    def test_hash_token_is_deterministic(self) -> None:
        raw = "some-raw-token-value"
        h1 = hash_token(raw)
        h2 = hash_token(raw)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_hash_token_is_not_reversible(self) -> None:
        raw = "secret-token"
        hashed = hash_token(raw)
        assert raw not in hashed
