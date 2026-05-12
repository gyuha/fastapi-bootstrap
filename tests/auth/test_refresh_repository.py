"""Refresh-token repository concurrency contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from domains.auth.repository import AuthRepository


class _ScalarResult:
    def scalar_one_or_none(self) -> None:
        return None


class _CapturingSession:
    def __init__(self) -> None:
        self.statement: Any | None = None

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statement = statement
        return _ScalarResult()


async def test_get_refresh_token_by_jti_for_update_uses_row_level_lock() -> None:
    session = _CapturingSession()
    repo = AuthRepository(session)  # type: ignore[arg-type]

    await repo.get_refresh_token_by_jti_for_update("refresh-jti")

    assert session.statement is not None
    assert session.statement._for_update_arg is not None


async def test_mark_refresh_token_rotated_updates_status_and_metadata_together() -> None:
    session = _CapturingSession()
    repo = AuthRepository(session)  # type: ignore[arg-type]
    rotated_at = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)

    await repo.mark_refresh_token_rotated(
        "old-refresh-jti",
        replaced_by_jti="new-refresh-jti",
        rotated_at=rotated_at,
    )

    assert session.statement is not None
    compiled = session.statement.compile()
    statement_text = str(compiled)
    assert "revoked" in statement_text
    assert "revoked_at" in statement_text
    assert "rotated_at" in statement_text
    assert "replaced_by_jti" in statement_text
    assert compiled.params["revoked"] is True
    assert compiled.params["revoked_at"] == rotated_at
    assert compiled.params["rotated_at"] == rotated_at
    assert compiled.params["replaced_by_jti"] == "new-refresh-jti"


async def test_invalidate_all_user_sessions_revokes_active_refresh_session_rows() -> None:
    session = _CapturingSession()
    repo = AuthRepository(session)  # type: ignore[arg-type]
    user_id = uuid.uuid4()

    await repo.invalidate_all_user_sessions(user_id)

    assert session.statement is not None
    compiled = session.statement.compile()
    statement_text = str(compiled)
    assert "refresh_tokens" in statement_text
    assert "revoked" in statement_text
    assert "revoked_at" in statement_text
    assert compiled.params["revoked"] is True
    assert user_id in compiled.params.values()
