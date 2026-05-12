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

import pytest

from domains.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from domains.auth.service import AuthService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMAIL = "alice@example.com"
_PASSWORD = "Password1!"


def _sign_test_token(payload: dict[str, Any]) -> str:
    """Return a JWT signed with the template's configured test secret."""
    from jose import jwt

    from core.config import get_settings
    from domains.auth.security import JWT_ALGORITHM

    secret = get_settings().jwt_secret_key.get_secret_value()
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def _tamper_signature(token: str) -> str:
    """Return *token* with an invalid signature while preserving JWT shape."""
    header, payload, signature = token.split(".")
    replacement = "a" if signature[0] != "a" else "b"
    return f"{header}.{payload}.{replacement}{signature[1:]}"


class CapturingAuthEmailService:
    """In-memory application mail-service fake for auth service tests."""

    def __init__(self) -> None:
        self.verification_emails: list[tuple[str, str]] = []
        self.password_reset_emails: list[tuple[str, str]] = []
        self.fail_verification = False
        self.fail_password_reset = False

    async def send_verification_email(self, user_email: str, token: str) -> None:
        if self.fail_verification:
            raise RuntimeError("SMTP error")
        self.verification_emails.append((user_email, token))

    async def send_password_reset_email(self, user_email: str, token: str) -> None:
        if self.fail_password_reset:
            raise RuntimeError("SMTP error")
        self.password_reset_emails.append((user_email, token))


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

    async def test_signup_persists_normalized_identity_fields(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        user, _raw_token = await auth_service.signup(
            "  ALICE@EXAMPLE.COM  ",
            _PASSWORD,
            "  Alice Kim  ",
        )

        assert fake_repo.users["alice@example.com"] is user
        assert user.email == "alice@example.com"
        assert user.display_name == "Alice Kim"
        assert user.hashed_password is not None
        assert user.hashed_password != _PASSWORD
        assert user.is_verified is False
        assert user.is_active is True
        assert isinstance(user.created_at, datetime)

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

    async def test_signup_initializes_required_verification_state(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        started_at = datetime.now(UTC)

        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        ev = await fake_repo.get_email_verification_by_token(raw_token)

        assert user.is_verified is False
        assert ev is not None
        assert ev.user_id == user.id
        assert ev.token_hash == hash_token(raw_token)
        assert ev.token_hash != raw_token
        assert ev.used is False
        assert isinstance(ev.created_at, datetime)
        assert started_at <= ev.created_at <= datetime.now(UTC)
        assert ev.expires_at.tzinfo is not None
        assert (
            timedelta(hours=23, minutes=59)
            <= ev.expires_at - ev.created_at
            <= timedelta(
                hours=24,
                minutes=1,
            )
        )

    async def test_signup_duplicate_email_raises_conflict(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import ConflictError

        await auth_service.signup(_EMAIL, _PASSWORD)
        with pytest.raises(ConflictError, match="already exists"):
            await auth_service.signup(_EMAIL, "AnotherPass2!")

    async def test_signup_duplicate_email_uses_normalized_identity(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import ConflictError

        await auth_service.signup("alice@example.com", _PASSWORD, "Alice")

        with pytest.raises(ConflictError, match="alice@example.com"):
            await auth_service.signup(
                "  ALICE@EXAMPLE.COM  ",
                "AnotherPass2!",
                "Alice Duplicate",
            )

    async def test_signup_database_unique_violation_returns_deterministic_conflict(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        from core.exceptions import ConflictError

        async def create_user_raises_unique_violation(*args: Any, **kwargs: Any) -> Any:
            raise IntegrityError(
                statement="INSERT INTO users (email) VALUES (:email)",
                params={"email": "alice@example.com"},
                orig=Exception("duplicate key value violates unique constraint users_email_key"),
            )

        fake_repo.create_user = create_user_raises_unique_violation

        with pytest.raises(ConflictError) as exc_info:
            await auth_service.signup("  ALICE@EXAMPLE.COM  ", _PASSWORD, "Alice")

        assert exc_info.value.status_code == 409
        assert exc_info.value.message == "An account with email 'alice@example.com' already exists."

    async def test_signup_and_send_email_dispatches_verification_email_via_mail_service(
        self,
        fake_repo: Any,
        fake_redis: Any,
    ) -> None:
        """signup_and_send_email sends the raw verification token through mail service."""
        mail_service = CapturingAuthEmailService()
        service = AuthService(repo=fake_repo, redis=fake_redis, mail_service=mail_service)

        user = await service.signup_and_send_email(_EMAIL, _PASSWORD)

        assert user.email == _EMAIL.lower()
        assert len(mail_service.verification_emails) == 1
        sent_email, sent_token = mail_service.verification_emails[0]
        assert sent_email == _EMAIL
        assert await fake_repo.get_email_verification_by_token(sent_token) is not None

    async def test_signup_and_send_email_suppresses_mail_error(
        self,
        fake_repo: Any,
        fake_redis: Any,
    ) -> None:
        """signup_and_send_email should not raise even if email delivery fails."""
        mail_service = CapturingAuthEmailService()
        mail_service.fail_verification = True
        service = AuthService(repo=fake_repo, redis=fake_redis, mail_service=mail_service)

        user = await service.signup_and_send_email(_EMAIL, _PASSWORD)

        assert user.email == _EMAIL.lower()

    async def test_signup_user_cannot_login_until_email_is_verified(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        user, _raw_token = await auth_service.signup(_EMAIL, _PASSWORD)

        assert user.is_verified is False
        assert user.is_active is True
        with pytest.raises(UnauthorizedError, match="Email verification is required"):
            await auth_service.login(_EMAIL, _PASSWORD)


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
        from core.exceptions import UnauthorizedError

        with pytest.raises(UnauthorizedError, match="Invalid"):
            await auth_service.verify_email("nonexistent-token")

    async def test_verify_email_already_used_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)

        with pytest.raises(UnauthorizedError, match="already used"):
            await auth_service.verify_email(raw_token)

    async def test_verify_email_expired_token_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

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

    async def _signup_and_verify(self, auth_service: AuthService, fake_repo: Any) -> Any:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)
        return user

    async def test_authenticate_credentials_returns_stored_user_for_valid_password_hash(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        user = await self._signup_and_verify(auth_service, fake_repo)

        authenticated = await auth_service.authenticate_credentials(
            "  ALICE@EXAMPLE.COM  ",
            _PASSWORD,
        )

        assert authenticated is user

    async def test_authenticate_credentials_returns_none_for_wrong_password_hash(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        await self._signup_and_verify(auth_service, fake_repo)

        authenticated = await auth_service.authenticate_credentials(_EMAIL, "WrongPassword!")

        assert authenticated is None

    async def test_login_returns_token_pair(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        await self._signup_and_verify(auth_service, fake_repo)
        tokens = await auth_service.login(_EMAIL, _PASSWORD)

        assert "access_token" in tokens
        assert "refresh_token" in tokens
        assert tokens["token_type"] == "bearer"
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
        from core.exceptions import UnauthorizedError

        await self._signup_and_verify(auth_service, fake_repo)
        with pytest.raises(UnauthorizedError):
            await auth_service.login(_EMAIL, "WrongPassword!")

    async def test_login_unknown_email_raises_unauthorized(
        self,
        auth_service: AuthService,
    ) -> None:
        from core.exceptions import UnauthorizedError

        with pytest.raises(UnauthorizedError):
            await auth_service.login("nobody@example.com", _PASSWORD)

    async def test_login_inactive_user_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

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

    async def _get_tokens(self, auth_service: AuthService, fake_repo: Any) -> dict[str, Any]:
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

    async def test_refresh_success_returns_decodable_persisted_replacement_tokens(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        tokens = await self._get_tokens(auth_service, fake_repo)
        old_refresh_payload = decode_token(tokens["refresh_token"])

        new_tokens = await auth_service.refresh(tokens["refresh_token"])
        new_access_payload = decode_token(new_tokens["access_token"])
        new_refresh_payload = decode_token(new_tokens["refresh_token"])
        persisted_new_refresh = await fake_repo.get_refresh_token_by_jti(new_refresh_payload["jti"])

        assert new_tokens["token_type"] == "bearer"
        assert new_tokens["expires_in"] == 15 * 60
        assert new_access_payload["type"] == "access"
        assert new_refresh_payload["type"] == "refresh"
        assert new_access_payload["sub"] == old_refresh_payload["sub"]
        assert new_refresh_payload["sub"] == old_refresh_payload["sub"]
        assert new_access_payload["jti"] != old_refresh_payload["jti"]
        assert new_refresh_payload["jti"] != old_refresh_payload["jti"]
        assert persisted_new_refresh is not None
        assert persisted_new_refresh.revoked is False

    async def test_refresh_persists_rotated_refresh_hash_and_jwt_expiry(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        tokens = await self._get_tokens(auth_service, fake_repo)

        new_tokens = await auth_service.refresh(tokens["refresh_token"])
        new_refresh_payload = decode_token(new_tokens["refresh_token"])
        persisted_new_refresh = await fake_repo.get_refresh_token_by_jti(new_refresh_payload["jti"])

        assert persisted_new_refresh is not None
        assert persisted_new_refresh.token_hash == hash_token(new_refresh_payload["raw"])
        assert persisted_new_refresh.token_hash != new_refresh_payload["raw"]
        assert persisted_new_refresh.expires_at == datetime.fromtimestamp(
            new_refresh_payload["exp"],
            tz=UTC,
        )

    async def test_refresh_persists_old_session_status_with_rotation_metadata(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        tokens = await self._get_tokens(auth_service, fake_repo)
        old_refresh_payload = decode_token(tokens["refresh_token"])
        old_refresh_row = await fake_repo.get_refresh_token_by_jti(old_refresh_payload["jti"])

        new_tokens = await auth_service.refresh(tokens["refresh_token"])
        new_refresh_payload = decode_token(new_tokens["refresh_token"])

        assert old_refresh_row is not None
        assert old_refresh_row.revoked is True
        assert old_refresh_row.revoked_at is not None
        assert old_refresh_row.rotated_at is not None
        assert old_refresh_row.revoked_at == old_refresh_row.rotated_at
        assert old_refresh_row.replaced_by_jti == new_refresh_payload["jti"]

    async def test_refresh_rotation_runs_in_transaction_and_locks_current_token(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        tokens = await self._get_tokens(auth_service, fake_repo)
        refresh_payload = decode_token(tokens["refresh_token"])

        await auth_service.refresh(tokens["refresh_token"])

        assert fake_repo.transaction_entries == 1
        assert fake_repo.transaction_exits == 1
        assert fake_repo.locked_refresh_jtis == [refresh_payload["jti"]]

    async def test_refresh_revokes_old_token(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        tokens = await self._get_tokens(auth_service, fake_repo)
        old_refresh = tokens["refresh_token"]
        old_refresh_payload = decode_token(old_refresh)
        old_refresh_row = await fake_repo.get_refresh_token_by_jti(old_refresh_payload["jti"])

        assert old_refresh_row is not None
        assert old_refresh_row.revoked is False

        new_tokens = await auth_service.refresh(old_refresh)
        new_refresh_payload = decode_token(new_tokens["refresh_token"])
        new_refresh_row = await fake_repo.get_refresh_token_by_jti(new_refresh_payload["jti"])

        assert old_refresh_row.revoked is True
        assert new_refresh_row is not None
        assert new_refresh_row.revoked is False

        # Old token should now be revoked — reusing it should fail
        with pytest.raises(UnauthorizedError, match="reuse detected"):
            await auth_service.refresh(old_refresh)

    async def test_refresh_reuse_detection_revokes_all_sessions(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        """Using a revoked token should revoke ALL family tokens."""
        from core.exceptions import UnauthorizedError

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

    async def test_refresh_reuse_detection_invalidates_user_session_records(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        """A reuse event must invalidate the user's server-side session records."""
        from core.exceptions import UnauthorizedError

        tokens = await self._get_tokens(auth_service, fake_repo)
        refresh_payload = decode_token(tokens["refresh_token"])
        old_refresh = tokens["refresh_token"]
        await auth_service.refresh(old_refresh)

        with pytest.raises(UnauthorizedError, match="reuse detected"):
            await auth_service.refresh(old_refresh)

        assert fake_repo.invalidated_session_user_ids == [refresh_payload["sub"]]

    async def test_rotated_refresh_token_reuse_is_classified_as_token_reuse_event(
        self,
        auth_service: AuthService,
        fake_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reusing a rotated refresh token is classified as a token-reuse security event."""
        from core.exceptions import UnauthorizedError
        from domains.auth.service import auth_service as auth_service_module

        tokens = await self._get_tokens(auth_service, fake_repo)
        old_refresh = tokens["refresh_token"]
        old_refresh_payload = decode_token(old_refresh)
        new_tokens = await auth_service.refresh(old_refresh)
        new_refresh_payload = decode_token(new_tokens["refresh_token"])

        warning_events: list[tuple[str, dict[str, Any]]] = []

        class CapturingLogger:
            def warning(self, event: str, **kwargs: Any) -> None:
                warning_events.append((event, kwargs))

        monkeypatch.setattr(auth_service_module, "logger", CapturingLogger())

        with pytest.raises(UnauthorizedError, match="reuse detected"):
            await auth_service.refresh(old_refresh)

        assert warning_events[-1] == (
            "refresh_token_reuse_detected",
            {
                "security_event_type": "token_reuse",
                "token_state": "rotated",
                "jti": old_refresh_payload["jti"],
                "user_id": old_refresh_payload["sub"],
                "family_id": old_refresh_payload["fid"],
                "replaced_by_jti": new_refresh_payload["jti"],
            },
        )

    async def test_revoked_refresh_token_reuse_is_classified_as_token_reuse_event(
        self,
        auth_service: AuthService,
        fake_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reusing a revoked non-rotated refresh token is classified as token reuse."""
        from core.exceptions import UnauthorizedError
        from domains.auth.service import auth_service as auth_service_module

        tokens = await self._get_tokens(auth_service, fake_repo)
        refresh_payload = decode_token(tokens["refresh_token"])
        await auth_service.logout(tokens["refresh_token"])

        warning_events: list[tuple[str, dict[str, Any]]] = []

        class CapturingLogger:
            def warning(self, event: str, **kwargs: Any) -> None:
                warning_events.append((event, kwargs))

        monkeypatch.setattr(auth_service_module, "logger", CapturingLogger())

        with pytest.raises(UnauthorizedError, match="reuse detected"):
            await auth_service.refresh(tokens["refresh_token"])

        assert warning_events[-1] == (
            "refresh_token_reuse_detected",
            {
                "security_event_type": "token_reuse",
                "token_state": "revoked",
                "jti": refresh_payload["jti"],
                "user_id": refresh_payload["sub"],
                "family_id": refresh_payload["fid"],
                "replaced_by_jti": None,
            },
        )

    async def test_refresh_invalid_token_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        with pytest.raises(UnauthorizedError):
            await auth_service.refresh("not-a-valid-jwt")

    async def test_refresh_rejects_token_with_tampered_signature(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        tokens = await self._get_tokens(auth_service, fake_repo)
        tampered_refresh = _tamper_signature(tokens["refresh_token"])

        with pytest.raises(UnauthorizedError, match="Invalid or expired refresh token"):
            await auth_service.refresh(tampered_refresh)

    async def test_refresh_rejects_expired_refresh_jwt_before_rotation(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        expired_payload = {
            "sub": "00000000-0000-4000-8000-000000000001",
            "jti": "expired-refresh-jti",
            "iat": datetime.now(UTC) - timedelta(days=8),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
            "type": "refresh",
            "fid": "expired-family-id",
            "raw": "expired-raw-refresh-token",
        }
        expired_refresh = _sign_test_token(expired_payload)

        with pytest.raises(UnauthorizedError, match="Invalid or expired refresh token"):
            await auth_service.refresh(expired_refresh)

    async def test_refresh_rejects_access_token_type(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        tokens = await self._get_tokens(auth_service, fake_repo)

        with pytest.raises(UnauthorizedError, match="not a refresh token"):
            await auth_service.refresh(tokens["access_token"])

    async def test_refresh_rejects_invalid_subject_identifier(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        tokens = await self._get_tokens(auth_service, fake_repo)
        payload = decode_token(tokens["refresh_token"])
        payload["sub"] = "not-a-uuid"
        invalid_subject_refresh = _sign_test_token(payload)

        with pytest.raises(UnauthorizedError, match="Invalid refresh token subject"):
            await auth_service.refresh(invalid_subject_refresh)

    async def test_refresh_rejects_subject_that_does_not_match_persisted_token_user(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        import uuid

        from core.exceptions import UnauthorizedError

        tokens = await self._get_tokens(auth_service, fake_repo)
        payload = decode_token(tokens["refresh_token"])
        payload["sub"] = str(uuid.uuid4())
        mismatched_subject_refresh = _sign_test_token(payload)

        with pytest.raises(UnauthorizedError, match="subject does not match"):
            await auth_service.refresh(mismatched_subject_refresh)


# ---------------------------------------------------------------------------
# TestLogout
# ---------------------------------------------------------------------------


class TestLogout:
    """logout() — refresh token revocation + access token blacklisting."""

    async def _get_tokens(self, auth_service: AuthService, fake_repo: Any) -> dict[str, Any]:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)
        return await auth_service.login(_EMAIL, _PASSWORD)

    async def test_logout_revokes_refresh_token(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

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

    async def test_logout_blacklist_ttl_matches_remaining_access_token_lifetime(
        self,
        auth_service: AuthService,
        fake_repo: Any,
        fake_redis: Any,
    ) -> None:
        tokens = await self._get_tokens(auth_service, fake_repo)
        access_payload = decode_token(tokens["access_token"])
        jti = access_payload["jti"]
        access_expires_at = datetime.fromtimestamp(access_payload["exp"], tz=UTC)
        expected_ttl = int((access_expires_at - datetime.now(UTC)).total_seconds())

        await auth_service.logout(
            tokens["refresh_token"],
            access_jti=jti,
            access_expires_at=access_expires_at,
        )

        stored_ttl = fake_redis.expirations[f"jwt:blacklist:{jti}"]
        assert expected_ttl - 2 <= stored_ttl <= expected_ttl
        assert stored_ttl < 15 * 60 + 60

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

    async def _registered_user(self, auth_service: AuthService, fake_repo: Any) -> Any:
        user, raw_token = await auth_service.signup(_EMAIL, _PASSWORD)
        await auth_service.verify_email(raw_token)
        return user

    async def test_request_password_reset_sends_email(
        self,
        fake_repo: Any,
        fake_redis: Any,
    ) -> None:
        mail_service = CapturingAuthEmailService()
        service = AuthService(repo=fake_repo, redis=fake_redis, mail_service=mail_service)
        await self._registered_user(service, fake_repo)

        await service.request_password_reset(_EMAIL)

        assert len(mail_service.password_reset_emails) == 1
        sent_email, sent_token = mail_service.password_reset_emails[0]
        assert sent_email == _EMAIL
        assert await fake_repo.get_password_reset_by_token(sent_token) is not None

    async def test_request_password_reset_uses_canonical_user_email_and_one_time_token(
        self,
        fake_repo: Any,
        fake_redis: Any,
    ) -> None:
        mail_service = CapturingAuthEmailService()
        service = AuthService(repo=fake_repo, redis=fake_redis, mail_service=mail_service)
        user = await self._registered_user(service, fake_repo)

        await service.request_password_reset("  ALICE@EXAMPLE.COM  ")

        assert len(fake_repo.password_resets) == 1
        sent_email, sent_token = mail_service.password_reset_emails[0]
        reset_row = await fake_repo.get_password_reset_by_token(sent_token)
        assert sent_email == user.email
        assert reset_row is not None
        assert reset_row.user_id == user.id
        assert reset_row.used is False
        assert reset_row.token_hash == hash_token(sent_token)
        assert reset_row.token_hash != sent_token
        assert len(sent_token) >= 43

    async def test_request_password_reset_unknown_email_silent(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        """No error for unknown email (prevents user enumeration)."""
        # Should not raise
        await auth_service.request_password_reset("nobody@example.com")

    async def test_request_password_reset_replaces_prior_unused_one_time_token(
        self,
        fake_repo: Any,
        fake_redis: Any,
    ) -> None:
        mail_service = CapturingAuthEmailService()
        service = AuthService(repo=fake_repo, redis=fake_redis, mail_service=mail_service)
        await self._registered_user(service, fake_repo)

        first_requested_at = datetime.now(UTC)
        await service.request_password_reset(_EMAIL)
        second_requested_at = datetime.now(UTC)
        await service.request_password_reset(_EMAIL)

        assert len(mail_service.password_reset_emails) == 2
        first_token = mail_service.password_reset_emails[0][1]
        second_token = mail_service.password_reset_emails[1][1]
        assert first_token != second_token

        first_row = await fake_repo.get_password_reset_by_token(first_token)
        second_row = await fake_repo.get_password_reset_by_token(second_token)
        assert first_row is not None
        assert second_row is not None
        assert first_row.used is True
        assert second_row.used is False
        assert first_requested_at + timedelta(minutes=55) <= first_row.expires_at
        assert second_requested_at + timedelta(minutes=55) <= second_row.expires_at

    async def test_confirm_password_reset_changes_password(
        self,
        fake_repo: Any,
        fake_redis: Any,
    ) -> None:
        mail_service = CapturingAuthEmailService()
        service = AuthService(repo=fake_repo, redis=fake_redis, mail_service=mail_service)
        await self._registered_user(service, fake_repo)

        await service.request_password_reset(_EMAIL)

        # Find the reset token
        pr_rows = list(fake_repo.password_resets.values())
        assert len(pr_rows) == 1
        assert len(mail_service.password_reset_emails) == 1
        raw_token = mail_service.password_reset_emails[0][1]

        new_password = "NewPassword2!"
        await service.confirm_password_reset(raw_token, new_password)

        # Old password should no longer work
        from core.exceptions import UnauthorizedError

        with pytest.raises(UnauthorizedError):
            await service.login(_EMAIL, _PASSWORD)

        # New password should work
        tokens = await service.login(_EMAIL, new_password)
        assert "access_token" in tokens

    async def test_confirm_invalid_token_raises_unauthorized(
        self,
        auth_service: AuthService,
        fake_repo: Any,
    ) -> None:
        from core.exceptions import UnauthorizedError

        with pytest.raises(UnauthorizedError, match="Invalid"):
            await auth_service.confirm_password_reset("invalid-token", "NewPass1!")


# ---------------------------------------------------------------------------
# TestRBAC
# ---------------------------------------------------------------------------


class TestRBAC:
    """RBAC — require_permission dependency."""

    def test_has_permission_returns_true_for_granted_permission(self) -> None:
        from unittest.mock import MagicMock

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
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from domains.auth.security import require_permission

        user = MagicMock()
        user.id = "test-user-id"
        user.has_permission = lambda key: False  # no permissions

        dep = require_permission("chat:write")

        with pytest.raises(HTTPException) as exc_info:
            await dep(user=user)

        assert exc_info.value.status_code == 403
        assert "chat:write" in exc_info.value.detail

    async def test_require_permission_returns_user_when_granted(self) -> None:
        from unittest.mock import MagicMock

        from domains.auth.security import require_permission

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
        import uuid

        user_id = str(uuid.uuid4())
        token = create_access_token(user_id)
        payload = decode_token(token)

        assert payload["sub"] == user_id
        assert payload["type"] == "access"
        assert "jti" in payload
        assert "exp" in payload

    def test_create_access_token_sets_15_minute_exp_claim(self) -> None:
        token = create_access_token("user-id")
        payload = decode_token(token)

        assert payload["exp"] - payload["iat"] == 15 * 60

    def test_create_access_token_reserved_claims_cannot_be_overridden(self) -> None:
        token = create_access_token(
            "user-id",
            jti="canonical-jti",
            extra_claims={
                "sub": "attacker-id",
                "jti": None,
                "iat": 0,
                "exp": 0,
                "type": "refresh",
                "scope": "chat:write",
            },
        )
        payload = decode_token(token)

        assert payload["sub"] == "user-id"
        assert payload["jti"] == "canonical-jti"
        assert payload["type"] == "access"
        assert payload["exp"] - payload["iat"] == 15 * 60
        assert payload["scope"] == "chat:write"

    def test_create_refresh_token_sets_7_day_exp_claim(self) -> None:
        token, _jti, _family_id = create_refresh_token("user-id")
        payload = decode_token(token)

        assert payload["exp"] - payload["iat"] == 7 * 24 * 60 * 60

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
