"""Development server hot-reload configuration validation.

Verifies that the project is correctly wired for uvicorn hot-reload:

* Makefile and Justfile ``dev`` / ``serve`` targets include ``uv run`` plus
  ``--reload`` and ``--reload-dir`` flags so that source file changes
  automatically restart the server without a manual kill/restart cycle.
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
_JUSTFILE = _PROJECT_ROOT / "Justfile"
_COMPOSE_FILE = _PROJECT_ROOT / "docker-compose.yml"
_MAIN_PY = _PROJECT_ROOT / "src" / "main.py"
_MAIN_MODULE = _PROJECT_ROOT / "src" / "__main__.py"
_SMOKE_TEST = _PROJECT_ROOT / "scripts" / "smoke_test.py"


def _makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _justfile_text() -> str:
    return _JUSTFILE.read_text(encoding="utf-8")


def _compose_text() -> str:
    return _COMPOSE_FILE.read_text(encoding="utf-8")


def _main_text() -> str:
    return _MAIN_PY.read_text(encoding="utf-8")


def _main_module_text() -> str:
    return _MAIN_MODULE.read_text(encoding="utf-8")


def _smoke_test_text() -> str:
    return _SMOKE_TEST.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_compose_service(service: str) -> str:
    """Return a top-level docker-compose service block by service name."""
    text = _compose_text()
    match = re.search(
        rf"^  {re.escape(service)}:\n(?P<body>(?:^    .*\n|^\s*$)+)",
        text,
        re.MULTILINE,
    )
    assert match is not None, f"docker-compose.yml must define a '{service}' service."
    return match.group(0)


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


def _extract_just_recipe(recipe: str) -> str:
    """Return the recipe lines belonging to a Justfile recipe."""
    lines = _justfile_text().splitlines()
    in_recipe = False
    recipe_lines: list[str] = []
    for line in lines:
        if re.match(rf"^{re.escape(recipe)}(?:\s|:)", line):
            in_recipe = True
            recipe_lines.append(line)
            continue
        if in_recipe:
            # A new Just recipe starts at column 0 and ends the current recipe.
            if line and not line[0].isspace() and not line.startswith("#"):
                break
            recipe_lines.append(line)
    return "\n".join(recipe_lines)


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
        assert "--reload-dir" in block, "Makefile 'serve' target must include '--reload-dir'."

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

    def test_dev_target_passes_host(self) -> None:
        """'make dev' must pass --host to uvicorn so the server binds 0.0.0.0."""
        block = _extract_target_block("dev")
        assert "--host" in block, (
            "Makefile 'dev' target must pass '--host' to uvicorn so the server "
            "binds all interfaces (0.0.0.0) and is reachable from the container "
            "network as well as the host browser."
        )

    def test_dev_target_passes_port(self) -> None:
        """'make dev' must pass --port to uvicorn so the port is configurable."""
        block = _extract_target_block("dev")
        assert "--port" in block, (
            "Makefile 'dev' target must pass '--port' to uvicorn so the "
            "listening port can be configured via the cookiecutter variable."
        )

    def test_host_variable_defaults_to_all_interfaces(self) -> None:
        """The HOST Makefile variable must default to 0.0.0.0."""
        makefile = _makefile_text()
        # Look for HOST := 0.0.0.0 or HOST = 0.0.0.0
        assert re.search(r"HOST\s*:?=\s*0\.0\.0\.0", makefile), (
            "Makefile HOST variable must default to '0.0.0.0' so that the "
            "uvicorn process binds all network interfaces by default."
        )


# ---------------------------------------------------------------------------
# Justfile — hot-reload flags present in dev recipes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJustfileHotReload:
    """Justfile recipes must mirror Makefile host-run hot-reload behaviour."""

    def test_justfile_exists(self) -> None:
        """Generated projects should offer just dev as an equivalent DX path."""
        assert _JUSTFILE.exists(), "Justfile must exist so 'just dev' is available."

    def test_dev_recipe_uses_uv_run(self) -> None:
        """'just dev' must run uvicorn through uv."""
        block = _extract_just_recipe("dev")
        assert "uv run uvicorn" in block, (
            "Justfile 'dev' recipe must invoke uvicorn through 'uv run' so it uses "
            "the project virtualenv on the host."
        )

    def test_dev_recipe_has_reload_flags(self) -> None:
        """'just dev' must enable and scope uvicorn hot-reload."""
        block = _extract_just_recipe("dev")
        assert "--reload" in block
        assert "--reload-dir" in block

    def test_serve_recipe_uses_uv_run_and_reload_flags(self) -> None:
        """'just serve' must be the no-bootstrap hot-reload shortcut."""
        block = _extract_just_recipe("serve")
        assert "uv run uvicorn" in block
        assert "--reload" in block
        assert "--reload-dir" in block

    def test_dev_recipe_passes_host_and_port(self) -> None:
        """'just dev' must pass the cookiecutter-configured bind host and port."""
        block = _extract_just_recipe("dev")
        assert "--host" in block
        assert "--port" in block


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
        assert "redis:" in _compose_text(), "docker-compose.yml must define a 'redis' service."

    def test_mailpit_service_defined(self) -> None:
        assert "mailpit:" in _compose_text(), "docker-compose.yml must define a 'mailpit' service."

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

    def test_postgres_exposes_local_dev_port_and_credentials(self) -> None:
        """Postgres must publish localhost-only port and dev credentials."""
        postgres_section = _extract_compose_service("postgres")
        assert '"127.0.0.1:${POSTGRES_PORT:-5432}:5432"' in postgres_section, (
            "The postgres service must bind the configured host port on 127.0.0.1 "
            "to container port 5432 for host-run local development."
        )
        assert "POSTGRES_USER: ${POSTGRES_USER:-app}" in postgres_section
        assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-app}" in postgres_section
        assert "POSTGRES_DB: ${POSTGRES_DB:-app_db}" in postgres_section

    def test_redis_exposes_local_dev_port(self) -> None:
        """Redis must publish a configurable localhost-only dev port."""
        redis_section = _extract_compose_service("redis")
        assert '"127.0.0.1:${REDIS_PORT:-6379}:6379"' in redis_section, (
            "The redis service must map REDIS_PORT on localhost to container port 6379 "
            "so the host-run FastAPI process can connect during local development "
            "without exposing Redis on every network interface."
        )

    def test_redis_has_environment_variables(self) -> None:
        """Redis service metadata must expose the local logical Redis settings."""
        redis_section = _extract_compose_service("redis")
        assert "environment:" in redis_section, (
            "The redis service must define environment variables for local dev diagnostics."
        )
        for variable in ("REDIS_HOST:", "REDIS_PORT:", "REDIS_DB:", "REDIS_URL:"):
            assert variable in redis_section, (
                f"The redis service environment must include {variable.rstrip(':')}."
            )

    def test_redis_has_healthcheck(self) -> None:
        """Redis must have a healthcheck for the same readiness reason."""
        redis_section = _extract_compose_service("redis")
        assert "healthcheck" in redis_section, "The redis service must define a healthcheck."
        assert "redis-cli" in redis_section and "ping" in redis_section, (
            "The redis healthcheck must use redis-cli ping to verify readiness."
        )

    def test_mailpit_exposes_localhost_ports_and_healthcheck(self) -> None:
        """Mailpit must publish SMTP/UI localhost-only ports and readiness probe."""
        mailpit_section = _extract_compose_service("mailpit")
        assert '"127.0.0.1:${MAILPIT_SMTP_PORT:-1025}:1025"' in mailpit_section
        assert '"127.0.0.1:${MAILPIT_UI_PORT:-8025}:8025"' in mailpit_section
        assert "MP_SMTP_AUTH_ACCEPT_ANY: 1" in mailpit_section
        assert "MP_SMTP_AUTH_ALLOW_INSECURE: 1" in mailpit_section
        assert 'test: ["CMD", "/mailpit", "readyz"]' in mailpit_section


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
        assert "def create_app()" in source, "main.py must define a create_app() factory function."

    def test_main_block_exists(self) -> None:
        """main.py must have an if __name__ == '__main__' block."""
        source = _main_text()
        has_main = '__name__ == "__main__"' in source or "__name__ == '__main__'" in source
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

    def test_ready_endpoint_checks_postgres_redis_and_mailpit(self) -> None:
        """/ready must prove the host-run app can reach all local dev services."""
        source = _main_text()
        assert '@health_router.get("/ready"' in source
        assert 'checks[name] = "ok"' in source
        assert "postgres" in source, "/ready must include the PostgreSQL dependency check."
        assert "SELECT 1" in source, (
            "/ready must execute a PostgreSQL SELECT 1 through the async engine."
        )
        assert "redis" in source, "/ready must include the Redis dependency check."
        assert "_await_if_needed(redis.ping())" in source, (
            "/ready must ping Redis using the configured REDIS_URL/host/port."
        )
        assert "mailpit" in source, "/ready must include the Mailpit dependency check."
        assert "asyncio.open_connection" in source, (
            "/ready must open an SMTP connection to Mailpit using MAIL_SERVER/MAIL_PORT."
        )
        assert "response.status_code = 503" in source, (
            "/ready must fail closed with HTTP 503 when a dependency is unavailable."
        )

    def test_smoke_test_calls_readiness_endpoint(self) -> None:
        """scripts/smoke_test.py must validate /ready before auth/chat flows."""
        source = _smoke_test_text()
        assert 'ready_url = f"{cfg.base_url}/ready"' in source
        for dependency in ("postgres", "redis", "mailpit"):
            assert dependency in source, (
                f"smoke_test.py must assert the {dependency} readiness check is ok."
            )

    def test_main_block_uses_settings_host(self) -> None:
        """The __main__ block must use settings.host — not a hardcoded '127.0.0.1'."""
        source = _main_text()
        assert "settings.host" in source, (
            "main.py __main__ block must use 'settings.host' for the uvicorn "
            "host parameter so the binding is driven by the HOST env var (default "
            "0.0.0.0 from cookiecutter) rather than a hardcoded loopback address."
        )

    def test_main_block_uses_settings_port(self) -> None:
        """The __main__ block must use settings.port — not a hardcoded integer."""
        source = _main_text()
        assert "settings.port" in source, (
            "main.py __main__ block must use 'settings.port' for the uvicorn "
            "port parameter so it respects the PORT env var."
        )


# ---------------------------------------------------------------------------
# __main__.py — python -m <package> module execution support
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMainModuleEntryPoint:
    """__main__.py must exist and mirror the uvicorn configuration of main.py.

    This enables running the app as ``uv run python -m app``,
    which is equivalent to the explicit uvicorn invocation in the Makefile.
    """

    def test_main_module_file_exists(self) -> None:
        """__main__.py must exist to support python -m <package> execution."""
        assert _MAIN_MODULE.exists(), (
            f"__main__.py not found at {_MAIN_MODULE}. "
            "The package must define __main__.py so it can be invoked with "
            "'uv run python -m app'."
        )

    def test_main_module_calls_uvicorn_run(self) -> None:
        """__main__.py must call uvicorn.run() when executed as __main__."""
        source = _main_module_text()
        assert "uvicorn.run(" in source, (
            "__main__.py must call uvicorn.run() to start the server when "
            "the package is invoked as a module."
        )

    def test_main_module_uses_settings_host(self) -> None:
        """__main__.py must use settings.host (not a hardcoded address)."""
        source = _main_module_text()
        assert "settings.host" in source, (
            "__main__.py must pass settings.host to uvicorn.run() so the "
            "binding address is driven by the HOST env var (default 0.0.0.0)."
        )

    def test_main_module_uses_settings_port(self) -> None:
        """__main__.py must use settings.port (not a hardcoded port number)."""
        source = _main_module_text()
        assert "settings.port" in source, (
            "__main__.py must pass settings.port to uvicorn.run() so the "
            "listening port is driven by the PORT env var."
        )

    def test_main_module_uses_is_development_for_reload(self) -> None:
        """__main__.py must gate hot-reload on settings.is_development()."""
        source = _main_module_text()
        assert "is_development()" in source, (
            "__main__.py must use settings.is_development() to enable reload "
            "only in development, matching the behaviour of make dev."
        )

    def test_main_module_has_main_guard(self) -> None:
        """__main__.py must guard uvicorn.run() with if __name__ == '__main__'."""
        source = _main_module_text()
        has_main = '__name__ == "__main__"' in source or "__name__ == '__main__'" in source
        assert has_main, (
            "__main__.py must guard execution with 'if __name__ == \"__main__\":' "
            "so that merely importing the module doesn't launch uvicorn."
        )


# ---------------------------------------------------------------------------
# Settings — is_development() / is_production() control reload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDevelopmentModeSettings:
    """Verify that Settings correctly exposes the development/production flag
    that drives the hot-reload decision in main.py."""

    def setup_method(self) -> None:
        from core.config import get_settings

        get_settings.cache_clear()

    def teardown_method(self) -> None:
        from core.config import get_settings

        get_settings.cache_clear()

    def test_is_development_true_by_default(self) -> None:
        """APP_ENV defaults to 'development' — is_development() must be True."""
        from core.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.is_development() is True, (
            "Default APP_ENV is 'development'; is_development() must return True "
            "so that hot-reload is enabled out-of-the-box."
        )

    def test_is_production_false_by_default(self) -> None:
        """is_production() must be False in the default dev configuration."""
        from core.config import Settings

        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.is_production() is False

    def test_is_development_false_in_production(self) -> None:
        """is_development() must return False when APP_ENV=production."""
        from core.config import Settings

        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.is_development() is False
        assert s.is_production() is True

    def test_is_development_false_in_staging(self) -> None:
        """is_development() must return False for staging env."""
        from core.config import Settings

        with patch.dict(os.environ, {"APP_ENV": "staging"}, clear=False):
            s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.is_development() is False

    def test_reload_decision_matches_is_development(self) -> None:
        """The reload flag computed in main.py follows is_development() exactly."""
        from core.config import Settings

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
