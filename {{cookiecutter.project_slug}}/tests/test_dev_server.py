"""Development server hot-reload configuration validation.

Verifies that the project is correctly wired for uvicorn hot-reload:

* Makefile ``dev`` and ``serve`` targets both include ``--reload`` and
  ``--reload-dir`` flags so that source file changes automatically restart
  the server without a manual kill/restart cycle.
* ``docker-compose.yml`` defines ONLY infrastructure services (postgres,
  redis, mailpit) and does NOT define a FastAPI/app container — the app
  must run on the host via ``uv run uvicorn --reload``.
* ``main.py`` exports a module-level ``app`` object (uvicorn import target)
  **and** declares an ``if __name__ == "__main__":`` block that calls
  ``uvicorn.run()`` with ``reload`` driven by ``settings.is_development()``.
* ``Settings.is_development()`` returns ``True`` by default (APP_ENV defaults
  to ``development``) and ``False`` in production — ensuring that hot-reload
  is never accidentally active in prod.

All tests are pure unit / static-analysis: no network, no DB, no running
server required.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Project paths (resolved relative to this test file)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent  # generated project root
_MAKEFILE = _PROJECT_ROOT / "Makefile"
_COMPOSE_FILE = _PROJECT_ROOT / "docker-compose.yml"
_MAIN_PY = _PROJECT_ROOT / "src" / "{{ cookiecutter.package_name }}" / "main.py"


def _makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _compose_text() -> str:
    return _COMPOSE_FILE.read_text(encoding="utf-8")


def _main_text() -> str:
    return _MAIN_PY.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_target_block(target: str) -> str:
    """Return the recipe lines belonging to a Makefile target.

    Reads until the next non-indented target definition so that multi-line
    recipes are captured in full.
    """
    lines = _makefile_text().splitlines()
    in_target = False
    target_lines: list[str] = []
    for line in lines:
        if re.match(rf"^{re.escape(target)}\s*[:?!]", line):
            in_target = True
            target_lines.append(line)
            continue
        if in_target:
            # A new target starts at a line with no leading whitespace that
            # contains a colon — stop collecting.
            if line and not line[0].isspace() and ":" in line:
                break
            target_lines.append(line)
    return "\n".join(target_lines)


# ---------------------------------------------------------------------------
# Makefile — hot-reload flags present in dev targets
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMakefileHotReload:
    """Makefile dev targets must enable uvicorn hot-reload."""

    def test_dev_target_has_reload_flag(self) -> None:
        """'make dev' must pass --reload to uvicorn."""
        block = _extract_target_block("dev")
        assert "--reload" in block, (
            "Makefile 'dev' target must include '--reload' so that source file "
            "changes automatically restart the uvicorn process."
        )

    def test_dev_target_has_reload_dir(self) -> None:
        """'make dev' must scope file-watching to the source tree."""
        block = _extract_target_block("dev")
        assert "--reload-dir" in block, (
            "Makefile 'dev' target must include '--reload-dir' to restrict "
            "uvicorn's file-watcher to the package source directory and avoid "
            "spurious reloads from test output or .env changes."
        )

    def test_serve_target_has_reload_flag(self) -> None:
        """'make serve' must also pass --reload to uvicorn."""
        block = _extract_target_block("serve")
        assert "--reload" in block, (
            "Makefile 'serve' target must include '--reload' for hot-reload "
            "when infra is already running."
        )

    def test_serve_target_has_reload_dir(self) -> None:
        """'make serve' must scope file-watching to the source tree."""
        block = _extract_target_block("serve")
        assert "--reload-dir" in block, (
            "Makefile 'serve' target must include '--reload-dir'."
        )

    def test_dev_target_uses_uv_run(self) -> None:
        """uvicorn must be invoked through 'uv run' to use the project venv."""
        block = _extract_target_block("dev")
        # Makefile expands $(UV) to 'uv run'; check the variable reference
        assert "$(UV)" in block or "uv run" in block, (
            "Makefile 'dev' target must invoke uvicorn through 'uv run' "
            "(via the $(UV) variable) to guarantee the project virtualenv."
        )

    def test_reload_dir_points_to_src(self) -> None:
        """--reload-dir must point inside the src/ tree."""
        block = _extract_target_block("dev")
        # The target uses $(SRC_DIR) = src/$(PACKAGE), so look for that pattern
        assert "$(SRC_DIR)" in block or "src/" in block, (
            "Makefile 'dev' target's --reload-dir should point to the src/ "
            "subtree (e.g. src/<package>) to limit the file-watch scope."
        )


# ---------------------------------------------------------------------------
# docker-compose.yml — infrastructure-only (FastAPI runs on the host)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDockerComposeInfraOnly:
    """docker-compose.yml must define ONLY infrastructure services.

    The FastAPI application must NOT be defined as a compose service; it
    must run on the host machine via 'uv run uvicorn --reload'.  This
    design choice is documented in the Seed contract under infra_compose.
    """

    _REQUIRED_SERVICES = ("postgres", "redis", "mailpit")
    # Keywords that would indicate an accidentally added app container
    _APP_SERVICE_KEYWORDS = ("fastapi", "uvicorn", "gunicorn", "web")

    def test_postgres_service_defined(self) -> None:
        assert "postgres:" in _compose_text(), (
            "docker-compose.yml must define a 'postgres' service."
        )

    def test_redis_service_defined(self) -> None:
        assert "redis:" in _compose_text(), (
            "docker-compose.yml must define a 'redis' service."
        )

    def test_mailpit_service_defined(self) -> None:
        assert "mailpit:" in _compose_text(), (
            "docker-compose.yml must define a 'mailpit' service."
        )

    def test_no_app_service_defined(self) -> None:
        """Compose file must not define a service that runs the FastAPI app."""
        text = _compose_text()
        for keyword in self._APP_SERVICE_KEYWORDS:
            # Look for the keyword as a top-level service name (2-space indent)
            match = re.search(rf"^  {re.escape(keyword)}\s*:", text, re.MULTILINE)
            assert match is None, (
                f"docker-compose.yml must not define a '{keyword}' service. "
                "The FastAPI app must run on the host via 'uv run uvicorn --reload'."
            )

    def test_compose_header_documents_host_fastapi(self) -> None:
        """The compose file header must document that FastAPI runs on the host."""
        text = _compose_text()
        assert "--reload" in text, (
            "docker-compose.yml must contain a comment referencing '--reload' "
            "to document that FastAPI runs on the host with hot-reload enabled."
        )
        # Check that the comment mentions the host
        assert "host" in text.lower(), (
            "docker-compose.yml must document the host-side uvicorn execution."
        )

    def test_postgres_has_healthcheck(self) -> None:
        """Postgres must have a healthcheck so 'make dev' waits for readiness."""
        text = _compose_text()
        # Find the postgres section and verify healthcheck is present
        pg_start = text.find("postgres:")
        assert pg_start != -1
        pg_section = text[pg_start : pg_start + 600]
        assert "healthcheck" in pg_section, (
            "The postgres service must define a healthcheck so that 'make infra' "
            "can poll until the database is ready before starting the app."
        )

    def test_redis_has_healthcheck(self) -> None:
        """Redis must have a healthcheck for the same readiness reason."""
        text = _compose_text()
        redis_start = text.find("redis:")
        assert redis_start != -1
        redis_section = text[redis_start : redis_start + 400]
        assert "healthcheck" in redis_section, (
            "The redis service must define a healthcheck."
        )


# ---------------------------------------------------------------------------
# main.py — module-level app + __main__ block with hot-reload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMainEntryPoint:
    """main.py must export 'app' at module level and support direct execution
    with hot-reload enabled for development."""

    def test_module_level_app_defined(self) -> None:
        """main.py must assign 'app' at module level for uvicorn import."""
        source = _main_text()
        # Accept both typed and untyped assignment forms
        assert re.search(r"^app\s*[:=]", source, re.MULTILINE), (
            "main.py must define 'app' at module level so uvicorn can import "
            "it as '<package>.main:app'."
        )

    def test_create_app_factory_exists(self) -> None:
        """A create_app() factory function must build and return the FastAPI instance."""
        source = _main_text()
        assert "def create_app()" in source, (
            "main.py must define a create_app() factory function."
        )

    def test_main_block_exists(self) -> None:
        """main.py must have an if __name__ == '__main__' block."""
        source = _main_text()
        has_main = (
            '__name__ == "__main__"' in source
            or "__name__ == '__main__'" in source
        )
        assert has_main, (
            "main.py must have an 'if __name__ == \"__main__\":' block so the "
            "app can be run directly via 'python -m <package>'."
        )

    def test_main_block_calls_uvicorn_run(self) -> None:
        """The __main__ block must call uvicorn.run() to start the server."""
        source = _main_text()
        assert "uvicorn.run(" in source, (
            "main.py __main__ block must call uvicorn.run() for direct execution."
        )

    def test_main_block_configures_reload(self) -> None:
        """The __main__ block must pass 'reload' to uvicorn.run()."""
        source = _main_text()
        assert "reload=" in source, (
            "main.py __main__ block must configure the 'reload' parameter in "
            "uvicorn.run() so that hot-reload is enabled/disabled based on env."
        )

    def test_main_block_reload_tied_to_is_development(self) -> None:
        """Hot-reload must be gated on settings.is_development()."""
        source = _main_text()
        assert "is_development()" in source, (
            "main.py __main__ block must use settings.is_development() to "
            "conditionally enable hot-reload, ensuring reload never runs in prod."
        )

    def test_main_block_configures_reload_dirs(self) -> None:
        """The __main__ block should restrict uvicorn's file-watcher to src/."""
        source = _main_text()
        assert "reload_dirs" in source, (
            "main.py __main__ block should pass 'reload_dirs' to uvicorn.run() "
            "to scope file watching to the package source directory."
        )

    def test_lifespan_registered(self) -> None:
        """The app must use a lifespan context manager (not deprecated on_event)."""
        source = _main_text()
        assert "lifespan" in source, (
            "main.py must register a lifespan context manager for startup/shutdown."
        )


# ---------------------------------------------------------------------------
# Settings — is_development() / is_production() control reload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDevelopmentModeSettings:
    """Verify that Settings correctly exposes the development/production flag
    that drives the hot-reload decision in main.py."""

    def setup_method(self) -> None:
        from {{ cookiecutter.package_name }}.core.config import get_settings

        get_settings.cache_clear()

    def teardown_method(self) -> None:
        from {{ cookiecutter.package_name }}.core.config import get_settings

        get_settings.cache_clear()

    def test_is_development_true_by_default(self) -> None:
        """APP_ENV defaults to 'development' — is_development() must be True."""
        from {{ cookiecutter.package_name }}.core.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.is_development() is True, (
            "Default APP_ENV is 'development'; is_development() must return True "
            "so that hot-reload is enabled out-of-the-box."
        )

    def test_is_production_false_by_default(self) -> None:
        """is_production() must be False in the default dev configuration."""
        from {{ cookiecutter.package_name }}.core.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.is_production() is False

    def test_is_development_false_in_production(self) -> None:
        """is_development() must return False when APP_ENV=production."""
        from {{ cookiecutter.package_name }}.core.config import Settings

        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.is_development() is False
        assert s.is_production() is True

    def test_is_development_false_in_staging(self) -> None:
        """is_development() must return False for staging env."""
        from {{ cookiecutter.package_name }}.core.config import Settings

        with patch.dict(os.environ, {"APP_ENV": "staging"}, clear=False):
            s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.is_development() is False

    def test_reload_decision_matches_is_development(self) -> None:
        """The reload flag computed in main.py follows is_development() exactly."""
        from {{ cookiecutter.package_name }}.core.config import Settings

        dev_settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert dev_settings.is_development() is True, "reload must be ON in dev"

        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            prod_settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert prod_settings.is_development() is False, "reload must be OFF in prod"

        # Demonstrate the exact conditional used in main.py:
        #   _reload = settings.is_development()
        #   uvicorn.run(..., reload=_reload, ...)
        assert (dev_settings.is_development()) is True
        assert (prod_settings.is_development()) is False
