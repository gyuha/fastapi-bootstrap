"""Alembic migration command contract tests.

These tests keep the local bootstrap path honest: ``make migrate`` must be a
standalone developer command that brings the local PostgreSQL dependency to a
healthy state and then applies the Alembic head revision through uv.

The tests are static by design. They do not require Docker or a live database,
but they pin the exact command wiring that makes the dynamic migration smoke
path work in generated projects.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_MAKEFILE = _PROJECT_ROOT / "Makefile"
_ALEMBIC_INI = _PROJECT_ROOT / "alembic.ini"
_ALEMBIC_VERSIONS = _PROJECT_ROOT / "alembic" / "versions"


def _makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _extract_make_target(target: str) -> str:
    """Return the Makefile target block, including the target declaration."""
    lines = _makefile_text().splitlines()
    in_target = False
    target_lines: list[str] = []
    for line in lines:
        if re.match(rf"^{re.escape(target)}\s*[:?!]", line):
            in_target = True
            target_lines.append(line)
            continue
        if in_target:
            if line and not line[0].isspace() and ":" in line:
                break
            target_lines.append(line)
    return "\n".join(target_lines)


@pytest.mark.unit
class TestMakeMigrate:
    """The generated Makefile must apply Alembic migrations safely."""

    def test_migrate_target_waits_for_local_postgres(self) -> None:
        """`make migrate` must be safe as a standalone local command."""
        target = _extract_make_target("migrate")
        first_line = target.splitlines()[0]

        assert "infra-health" in first_line, (
            "Makefile 'migrate' target must depend on 'infra-health' so running "
            "`make migrate` directly starts postgres/redis/mailpit and waits for "
            "PostgreSQL to be healthy before Alembic connects."
        )

    def test_migrate_target_applies_alembic_head_with_uv(self) -> None:
        """`make migrate` must execute Alembic upgrade head inside the uv env."""
        target = _extract_make_target("migrate")

        assert "$(UV) alembic upgrade head" in target or "uv run alembic upgrade head" in target

    def test_dev_bootstrap_runs_migrate_before_uvicorn(self) -> None:
        """`make dev` must apply migrations before launching the API server."""
        target = _extract_make_target("dev")
        first_line = target.splitlines()[0]

        assert "migrate" in first_line, "Makefile 'dev' target must depend on migrate."
        assert target.find("uvicorn") > target.find("dev:"), (
            "Makefile 'dev' target should launch uvicorn only after dependencies "
            "(including migrate) have completed."
        )

    def test_alembic_ini_points_to_template_migration_directory(self) -> None:
        """Alembic must resolve migrations from the checked-in alembic directory."""
        assert "script_location = alembic" in _ALEMBIC_INI.read_text(encoding="utf-8")

    def test_initial_revision_exists_for_upgrade_head(self) -> None:
        """`alembic upgrade head` needs at least one versioned migration to apply."""
        revisions = [path for path in _ALEMBIC_VERSIONS.glob("*.py") if path.name != "__init__.py"]

        assert revisions, "alembic/versions must contain an initial schema revision."
        assert any(
            'revision: str = "0001_initial_schema"' in p.read_text(encoding="utf-8")
            for p in revisions
        )
