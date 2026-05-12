"""Password-reset token persistence contract tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from domains.auth.models import PasswordReset
from domains.auth.repository import AuthRepository
from domains.auth.security import hash_token


class _ScalarResult:
    def __init__(self, value: Any | None = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any | None:
        return self._value


class _CapturingSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushed = False
        self.statement: Any | None = None

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        self.flushed = True

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statement = statement
        return _ScalarResult()


async def test_create_password_reset_persists_hashed_token_user_expiry_and_unused_state() -> None:
    session = _CapturingSession()
    repo = AuthRepository(session)  # type: ignore[arg-type]
    user_id = uuid.uuid4()
    raw_token = "raw-password-reset-token"
    expires_at = datetime.now(UTC) + timedelta(hours=1)

    row = await repo.create_password_reset(
        user_id=user_id,
        raw_token=raw_token,
        expires_at=expires_at,
    )

    assert isinstance(row, PasswordReset)
    assert session.added == [row]
    assert session.flushed is True
    assert row.user_id == user_id
    assert row.token_hash == hash_token(raw_token)
    assert row.token_hash != raw_token
    assert row.expires_at == expires_at
    assert row.used is False


async def test_get_password_reset_by_token_queries_by_hashed_token_only() -> None:
    session = _CapturingSession()
    repo = AuthRepository(session)  # type: ignore[arg-type]
    raw_token = "lookup-password-reset-token"

    await repo.get_password_reset_by_token(raw_token)

    assert session.statement is not None
    compiled = session.statement.compile(compile_kwargs={"literal_binds": True})
    statement_sql = str(compiled)
    assert hash_token(raw_token) in statement_sql
    assert raw_token not in statement_sql


async def test_mark_user_password_resets_used_updates_unused_rows_for_user() -> None:
    session = _CapturingSession()
    repo = AuthRepository(session)  # type: ignore[arg-type]
    user_id = uuid.uuid4()

    await repo.mark_user_password_resets_used(user_id)

    assert session.statement is not None
    compiled = session.statement.compile(compile_kwargs={"literal_binds": True})
    statement_sql = str(compiled)
    assert "password_resets" in statement_sql
    assert "used" in statement_sql
    assert user_id.hex in statement_sql


async def test_mark_password_reset_used_updates_used_flag_by_identifier() -> None:
    session = _CapturingSession()
    repo = AuthRepository(session)  # type: ignore[arg-type]
    reset_id = uuid.uuid4()

    await repo.mark_password_reset_used(reset_id)

    assert session.statement is not None
    compiled = session.statement.compile(compile_kwargs={"literal_binds": True})
    statement_sql = str(compiled)
    assert "password_resets" in statement_sql
    assert "used" in statement_sql
    assert reset_id.hex in statement_sql
