"""Tests for the application settings / configuration layer.

Pure unit tests — no I/O, no DB, no Redis.  They verify that:

* All required settings fields are present and have correct defaults.
* LLM-provider env-vars are correctly mapped to :class:`LLMProvider` values.
* Helper properties (``litellm_model``, ``as_litellm_kwargs``) return the
  expected strings for each supported provider.
* CORS origin parsing handles both list and comma-separated string inputs.
* ``get_settings()`` cache can be cleared and re-populated in tests.

Note on constructing BaseSettings in tests:
  Field names (not env var names) are used as keyword arguments.
  Aliases defined via ``Field(alias=...)`` are also accepted when
  ``populate_by_name=True`` is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import (
    AppEnv,
    LLMProvider,
    LLMSettings,
    LogFormat,
    Settings,
    get_settings,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(**env_overrides: str) -> Settings:
    """Create a Settings instance completely isolated from os.environ and .env.

    Uses ``clear=True`` to start from an empty environment, then adds only the
    required base keys and any caller-supplied overrides.  This prevents third-
    party libraries (e.g. *litellm*) that call ``load_dotenv()`` at import time
    from leaking ``.env`` values into subsequent tests.

    Pass env var names (uppercase) as keyword arguments, e.g.::

        make_settings(APP_ENV="production", WORKERS="4")
    """
    base_env: dict[str, str] = {
        "SECRET_KEY": "test-secret-key",
        "JWT_SECRET_KEY": "test-jwt-secret",
    }
    base_env.update(env_overrides)
    with patch.dict(os.environ, base_env, clear=True):
        get_settings.cache_clear()
        # _env_file=None prevents pydantic-settings from reading .env
        s = Settings(_env_file=None)  # type: ignore[call-arg]
    get_settings.cache_clear()
    return s


# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------


class TestAppSettings:
    def test_defaults(self) -> None:
        s = make_settings()
        assert s.app_env == AppEnv.development
        assert s.app_debug is False
        assert s.host == "0.0.0.0"
        assert s.port == 8000

    def test_env_override(self) -> None:
        s = make_settings(APP_ENV="production", APP_DEBUG="false", WORKERS="4")
        assert s.app_env == AppEnv.production
        assert s.is_production() is True
        assert s.is_development() is False
        assert s.workers == 4

    def test_cors_origins_from_comma_string(self) -> None:
        s = make_settings(CORS_ORIGINS="http://a.com,http://b.com, http://c.com")
        origins = s.cors_origins_list
        assert "http://a.com" in origins
        assert "http://b.com" in origins
        assert "http://c.com" in origins

    def test_cors_origins_from_json_array(self) -> None:
        s = make_settings(CORS_ORIGINS='["http://a.com","http://b.com"]')
        origins = s.cors_origins_list
        assert "http://a.com" in origins
        assert "http://b.com" in origins

    def test_log_level_uppercased(self) -> None:
        s = make_settings(LOG_LEVEL="debug")
        assert s.log_level == "DEBUG"

    def test_log_format_json_default(self) -> None:
        s = make_settings()
        assert s.log_format == LogFormat.json

    def test_frontend_reset_confirm_url_base_default(self) -> None:
        """Password-reset confirm links have an explicit frontend base URL setting."""
        s = make_settings()
        assert s.frontend_reset_confirm_url_base == ("http://localhost:3000/auth/reset-confirm")

    def test_frontend_reset_confirm_url_base_env_override_is_normalized(self) -> None:
        """FRONTEND_RESET_CONFIRM_URL_BASE can point reset emails at a custom frontend route."""
        s = make_settings(
            FRONTEND_RESET_CONFIRM_URL_BASE="https://app.example.com/reset-confirm/",
        )
        assert s.frontend_reset_confirm_url_base == "https://app.example.com/reset-confirm"


# ---------------------------------------------------------------------------
# Database DSN properties
# ---------------------------------------------------------------------------


class TestDatabaseDSN:
    def test_async_dsn_built_from_parts(self) -> None:
        s = make_settings(
            POSTGRES_USER="myuser",
            POSTGRES_PASSWORD="mypass",
            POSTGRES_HOST="db-host",
            POSTGRES_PORT="5433",
            POSTGRES_DB="mydb",
        )
        dsn = s.async_database_url
        assert dsn.startswith("postgresql+asyncpg://")
        assert "myuser:mypass@db-host:5433/mydb" in dsn

    def test_sync_dsn_uses_psycopg2(self) -> None:
        s = make_settings(
            POSTGRES_USER="u",
            POSTGRES_PASSWORD="p",
            POSTGRES_HOST="h",
            POSTGRES_PORT="5432",
            POSTGRES_DB="d",
        )
        assert "psycopg2" in s.sync_database_url

    def test_explicit_database_url_takes_precedence(self) -> None:
        explicit = "postgresql+asyncpg://custom:custom@custom-host/custom_db"
        s = make_settings(DATABASE_URL=explicit)
        assert s.async_database_url == explicit

    def test_explicit_sync_url_takes_precedence(self) -> None:
        explicit = "postgresql+psycopg2://u:p@h/d"
        s = make_settings(DATABASE_URL_SYNC=explicit)
        assert s.sync_database_url == explicit


# ---------------------------------------------------------------------------
# Redis DSN property
# ---------------------------------------------------------------------------


class TestRedisDSN:
    def test_dsn_built_from_parts(self) -> None:
        s = make_settings(REDIS_HOST="redis-host", REDIS_PORT="6380", REDIS_DB="1")
        assert s.redis_dsn == "redis://redis-host:6380/1"

    def test_explicit_redis_url_takes_precedence(self) -> None:
        explicit = "redis://custom-redis:6379/2"
        s = make_settings(REDIS_URL=explicit)
        assert s.redis_dsn == explicit


# ---------------------------------------------------------------------------
# JWT settings
# ---------------------------------------------------------------------------


class TestJWTSettings:
    def test_default_ttls(self) -> None:
        s = make_settings()
        assert s.jwt_access_token_expire_minutes == 15
        assert s.jwt_refresh_token_expire_days == 7
        assert s.jwt_algorithm == "HS256"

    def test_jwt_env_override(self) -> None:
        s = make_settings(
            JWT_ACCESS_TOKEN_EXPIRE_MINUTES="30",
            JWT_REFRESH_TOKEN_EXPIRE_DAYS="14",
        )
        assert s.jwt_access_token_expire_minutes == 30
        assert s.jwt_refresh_token_expire_days == 14


# ---------------------------------------------------------------------------
# Email / SMTP settings
# ---------------------------------------------------------------------------


class TestEmailSettings:
    """Verify SMTP defaults match Mailpit dev configuration and can be overridden."""

    def test_defaults_match_mailpit(self) -> None:
        """Default email settings point to Mailpit on localhost."""
        s = make_settings()
        assert s.mail_server == "localhost"
        assert s.mail_port == 1025
        assert s.mail_starttls is False
        assert s.mail_ssl_tls is False
        assert s.mail_username == ""
        # Mailpit accepts anonymous SMTP — no credentials needed in dev
        assert s.mail_password.get_secret_value() == ""

    def test_mail_from_uses_project_slug(self) -> None:
        s = make_settings()
        assert "fastapi-bootstrap" in s.mail_from
        assert s.mail_from_name == "FastAPI Bootstrap"

    def test_production_smtp_override(self) -> None:
        """Production SMTP credentials can be set via environment variables."""
        s = make_settings(
            MAIL_SERVER="smtp.sendgrid.net",
            MAIL_PORT="587",
            MAIL_USERNAME="apikey",
            MAIL_PASSWORD="SG.test-api-key",
            MAIL_FROM="no-reply@example.com",
            MAIL_FROM_NAME="Example App",
            MAIL_STARTTLS="true",
            MAIL_SSL_TLS="false",
        )
        assert s.mail_server == "smtp.sendgrid.net"
        assert s.mail_port == 587
        assert s.mail_username == "apikey"
        assert s.mail_password.get_secret_value() == "SG.test-api-key"
        assert s.mail_from == "no-reply@example.com"
        assert s.mail_from_name == "Example App"
        assert s.mail_starttls is True
        assert s.mail_ssl_tls is False

    def test_mail_connection_config_dev_defaults(self) -> None:
        """mail_connection_config returns correct kwargs for Mailpit (dev)."""
        s = make_settings()
        cfg = s.mail_connection_config

        assert cfg["MAIL_SERVER"] == "localhost"
        assert cfg["MAIL_PORT"] == 1025
        assert cfg["MAIL_STARTTLS"] is False
        assert cfg["MAIL_SSL_TLS"] is False
        # No credentials → USE_CREDENTIALS must be False for Mailpit
        assert cfg["USE_CREDENTIALS"] is False
        # No TLS → cert validation disabled
        assert cfg["VALIDATE_CERTS"] is False

    def test_mail_connection_config_production(self) -> None:
        """mail_connection_config enables credentials and cert validation for TLS."""
        s = make_settings(
            MAIL_SERVER="smtp.example.com",
            MAIL_PORT="587",
            MAIL_USERNAME="user@example.com",
            MAIL_PASSWORD="secret",
            MAIL_STARTTLS="true",
            MAIL_SSL_TLS="false",
        )
        cfg = s.mail_connection_config

        assert cfg["MAIL_SERVER"] == "smtp.example.com"
        assert cfg["MAIL_PORT"] == 587
        # Username is set → credentials required
        assert cfg["USE_CREDENTIALS"] is True
        # STARTTLS enabled → validate certs
        assert cfg["VALIDATE_CERTS"] is True
        assert cfg["MAIL_PASSWORD"] == "secret"

    def test_mail_connection_config_keys(self) -> None:
        """mail_connection_config contains all keys required by fastapi-mail."""
        s = make_settings()
        cfg = s.mail_connection_config
        required_keys = {
            "MAIL_USERNAME",
            "MAIL_PASSWORD",
            "MAIL_FROM",
            "MAIL_PORT",
            "MAIL_SERVER",
            "MAIL_FROM_NAME",
            "MAIL_STARTTLS",
            "MAIL_SSL_TLS",
            "USE_CREDENTIALS",
            "VALIDATE_CERTS",
        }
        assert required_keys.issubset(cfg.keys())

    def test_mail_server_can_target_compose_service_name(self) -> None:
        """Manual compose-network execution can target the Mailpit service name."""
        s = make_settings(MAIL_SERVER="mailpit", MAIL_PORT="1025")
        assert s.mail_server == "mailpit"
        assert s.mail_connection_config["MAIL_SERVER"] == "mailpit"


# ---------------------------------------------------------------------------
# Container connectivity — host vs docker-compose service names
# ---------------------------------------------------------------------------


class TestContainerConnectivity:
    """Verify that host/URL settings can be overridden for container-to-container networking.

    In local dev the FastAPI app runs on the *host* machine, so it uses
    ``localhost`` to reach the containers.  In production container mode
    (``docker-compose.prod.yml``), the app runs *inside* the compose network and
    must use container service names for infrastructure hosts while SMTP is
    injected from real provider env values.  These tests verify that environment
    variable overrides compose correctly with or without the full DSN shortcuts.
    """

    def test_postgres_host_override_for_container(self) -> None:
        """Setting POSTGRES_HOST=postgres uses the container service name in DSN."""
        s = make_settings(
            POSTGRES_HOST="postgres",
            POSTGRES_USER="app",
            POSTGRES_PASSWORD="app",
            POSTGRES_PORT="5432",
            POSTGRES_DB="app_db",
        )
        url = s.async_database_url
        assert "postgres:5432" in url
        assert url.startswith("postgresql+asyncpg://")

    def test_redis_host_override_for_container(self) -> None:
        """Setting REDIS_HOST=redis uses the container service name in DSN."""
        s = make_settings(REDIS_HOST="redis", REDIS_PORT="6379", REDIS_DB="0")
        assert s.redis_dsn == "redis://redis:6379/0"

    def test_full_database_url_overrides_component_vars(self) -> None:
        """Explicit DATABASE_URL takes precedence over POSTGRES_* component vars.

        This is how docker-compose overrides the URL for the ``app`` profile:
        it sets DATABASE_URL with the container hostname directly.
        """
        container_url = "postgresql+asyncpg://app:app@postgres:5432/app_db"
        s = make_settings(
            DATABASE_URL=container_url,
            POSTGRES_HOST="localhost",  # would be wrong — but overridden by DATABASE_URL
        )
        assert s.async_database_url == container_url

    def test_full_redis_url_overrides_component_vars(self) -> None:
        """Explicit REDIS_URL takes precedence over REDIS_* component vars."""
        container_url = "redis://redis:6379/0"
        s = make_settings(
            REDIS_URL=container_url,
            REDIS_HOST="localhost",  # would be wrong — but overridden by REDIS_URL
        )
        assert s.redis_dsn == container_url

    def test_sync_database_url_for_alembic_container(self) -> None:
        """DATABASE_URL_SYNC produces a psycopg2 DSN for Alembic in container mode."""
        container_sync_url = "postgresql+psycopg2://app:app@postgres:5432/app_db"
        s = make_settings(DATABASE_URL_SYNC=container_sync_url)
        assert s.sync_database_url == container_sync_url
        assert "psycopg2" in s.sync_database_url
        # Alembic must never get an asyncpg DSN
        assert "asyncpg" not in s.sync_database_url

    def test_local_dev_env_file_loads_compose_service_settings(
        self,
        tmp_path: Path,
    ) -> None:
        """Settings loads explicit local DSN/Redis/Mailpit values from a .env file."""
        env_file = tmp_path / ".env"
        async_dsn = "postgresql+asyncpg://app:app@localhost:5432/app_db"
        sync_dsn = "postgresql+psycopg2://app:app@localhost:5432/app_db"
        env_file.write_text(
            "\n".join(
                [
                    "APP_ENV=development",
                    f"DATABASE_URL={async_dsn}",
                    f"DATABASE_URL_SYNC={sync_dsn}",
                    "REDIS_URL=redis://localhost:6379/0",
                    "MAIL_SERVER=localhost",
                    "MAIL_PORT=1025",
                    "MAIL_USERNAME=",
                    "MAIL_PASSWORD=",
                    "MAIL_STARTTLS=false",
                    "MAIL_SSL_TLS=false",
                ]
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=env_file)  # type: ignore[call-arg]

        assert s.async_database_url == async_dsn
        assert s.sync_database_url == sync_dsn
        assert s.redis_dsn == "redis://localhost:6379/0"
        assert s.mail_connection_config["MAIL_SERVER"] == "localhost"
        assert s.mail_connection_config["MAIL_PORT"] == 1025
        assert s.mail_connection_config["USE_CREDENTIALS"] is False
        assert s.mail_connection_config["VALIDATE_CERTS"] is False

    def test_default_host_is_localhost(self) -> None:
        """Default configuration targets localhost (host-to-container dev mode)."""
        s = make_settings()
        assert s.postgres_host == "localhost"
        assert s.redis_host == "localhost"
        assert s.mail_server == "localhost"
        # Async DSN should contain localhost
        assert "localhost" in s.async_database_url
        assert "localhost" in s.redis_dsn


# ---------------------------------------------------------------------------
# LLM provider settings (LLMSettings standalone)
# ---------------------------------------------------------------------------


class TestLLMProvider:
    """Verify LLMProvider enum and LLMSettings helper methods for every provider."""

    def test_all_provider_values_in_enum(self) -> None:
        values = {p.value for p in LLMProvider}
        assert values == {"openai", "anthropic", "gemini", "azure", "ollama"}

    def test_provider_env_var_name_is_llm_provider(self) -> None:
        """LLM_PROVIDER is the canonical env var for provider selection."""
        s = LLMSettings(provider=LLMProvider.anthropic, default_model="claude-3-5-haiku-20241022")
        assert s.provider == LLMProvider.anthropic

    def test_invalid_provider_raises_with_helpful_message(self) -> None:
        """Unsupported LLM_PROVIDER should raise ValueError with all valid values listed."""
        with pytest.raises((ValueError, Exception)) as exc_info:
            LLMSettings(provider="cohere", default_model="command-r")  # type: ignore[arg-type]
        err = str(exc_info.value)
        # Must mention the bad value
        assert "cohere" in err
        # Must list valid providers
        assert "openai" in err

    def test_temperature_defaults_and_range(self) -> None:
        s = LLMSettings(provider=LLMProvider.openai, default_model="gpt-4o")
        assert 0.0 <= s.temperature <= 2.0
        assert s.temperature == 0.7

    def test_max_tokens_default(self) -> None:
        s = LLMSettings(provider=LLMProvider.openai, default_model="gpt-4o")
        assert s.max_tokens == 2048
        assert s.max_tokens > 0

    def test_streaming_default_matches_template_setting(self) -> None:
        s = LLMSettings(provider=LLMProvider.openai, default_model="gpt-4o")
        assert s.streaming is True

    def test_generation_params_in_litellm_kwargs(self) -> None:
        s = LLMSettings(
            provider=LLMProvider.openai,
            default_model="gpt-4o",
            OPENAI_API_KEY="sk-test",
            temperature=0.3,
            max_tokens=512,
            streaming=False,
        )
        kwargs = s.as_litellm_kwargs()
        assert kwargs["temperature"] == 0.3
        assert kwargs["max_tokens"] == 512
        assert kwargs["streaming"] is False

    def test_openai(self) -> None:
        s = LLMSettings(
            provider=LLMProvider.openai,
            default_model="gpt-4o-mini",
            OPENAI_API_KEY="sk-test",
        )
        assert s.litellm_model == "openai/gpt-4o-mini"
        kwargs = s.as_litellm_kwargs()
        assert kwargs["model"] == "openai/gpt-4o-mini"
        assert kwargs["api_key"] == "sk-test"

    def test_anthropic(self) -> None:
        s = LLMSettings(
            provider=LLMProvider.anthropic,
            default_model="claude-3-5-sonnet-20241022",
            ANTHROPIC_API_KEY="sk-ant-test",
        )
        assert s.litellm_model == "anthropic/claude-3-5-sonnet-20241022"
        assert s.as_litellm_kwargs()["api_key"] == "sk-ant-test"

    def test_gemini(self) -> None:
        s = LLMSettings(
            provider=LLMProvider.gemini,
            default_model="gemini-1.5-flash",
            GEMINI_API_KEY="AIza-test",
        )
        assert s.litellm_model == "gemini/gemini-1.5-flash"
        assert s.as_litellm_kwargs()["api_key"] == "AIza-test"

    def test_azure_uses_deployment_name(self) -> None:
        s = LLMSettings(
            provider=LLMProvider.azure,
            default_model="gpt-4o",
            AZURE_OPENAI_API_KEY="az-key",
            AZURE_OPENAI_ENDPOINT="https://my.openai.azure.com/",
            AZURE_OPENAI_DEPLOYMENT="my-deployment",
            AZURE_OPENAI_API_VERSION="2024-08-01-preview",
        )
        assert s.litellm_model == "azure/my-deployment"
        kwargs = s.as_litellm_kwargs()
        assert kwargs["api_base"] == "https://my.openai.azure.com/"
        assert kwargs["api_version"] == "2024-08-01-preview"
        assert kwargs["api_key"] == "az-key"

    def test_azure_falls_back_to_default_model_when_no_deployment(self) -> None:
        s = LLMSettings(
            provider=LLMProvider.azure,
            default_model="gpt-4o-mini",
            AZURE_OPENAI_DEPLOYMENT="",
        )
        assert s.litellm_model == "azure/gpt-4o-mini"

    def test_ollama_no_api_key(self) -> None:
        s = LLMSettings(
            provider=LLMProvider.ollama,
            default_model="llama3.2",
            OLLAMA_BASE_URL="http://localhost:11434",
        )
        assert s.litellm_model == "ollama/llama3.2"
        kwargs = s.as_litellm_kwargs()
        assert kwargs["api_base"] == "http://localhost:11434"
        assert kwargs["api_key"] == "ollama"

    def test_active_api_key_returns_correct_provider_key(self) -> None:
        s = LLMSettings(
            provider=LLMProvider.openai,
            default_model="gpt-4o",
            OPENAI_API_KEY="sk-real",
        )
        assert s.active_api_key == "sk-real"

    def test_ollama_active_api_key_is_empty(self) -> None:
        s = LLMSettings(provider=LLMProvider.ollama, default_model="llama3.2")
        assert s.active_api_key == ""


class TestLLMSettingsViaRootSettings:
    """Test .llm property — integration between root Settings and LLMSettings."""

    def test_llm_openai(self) -> None:
        s = make_settings(
            LLM_PROVIDER="openai",
            LLM_DEFAULT_MODEL="gpt-4o",
            OPENAI_API_KEY="sk-integration-test",
        )
        assert s.llm_provider == LLMProvider.openai
        llm = s.llm
        assert llm.litellm_model == "openai/gpt-4o"
        assert llm.active_api_key == "sk-integration-test"

    def test_llm_anthropic(self) -> None:
        s = make_settings(
            LLM_PROVIDER="anthropic",
            LLM_DEFAULT_MODEL="claude-3-5-sonnet-20241022",
            ANTHROPIC_API_KEY="sk-ant-integration",
        )
        assert s.llm.litellm_model == "anthropic/claude-3-5-sonnet-20241022"

    def test_llm_provider_is_enum_type(self) -> None:
        s = make_settings(LLM_PROVIDER="gemini", LLM_DEFAULT_MODEL="gemini-1.5-pro")
        assert isinstance(s.llm_provider, LLMProvider)
        assert s.llm_provider == LLMProvider.gemini

    def test_llm_ollama_via_settings(self) -> None:
        s = make_settings(
            LLM_PROVIDER="ollama",
            LLM_DEFAULT_MODEL="llama3.2",
            OLLAMA_BASE_URL="http://custom-ollama:11434",
        )
        llm = s.llm
        assert llm.litellm_model == "ollama/llama3.2"
        assert llm.as_litellm_kwargs()["api_base"] == "http://custom-ollama:11434"

    def test_invalid_llm_provider_raises_validation_error(self) -> None:
        with pytest.raises(Exception):  # pydantic ValidationError
            make_settings(LLM_PROVIDER="nonexistent-provider")

    def test_invalid_provider_error_message_lists_valid_providers(self) -> None:
        """ValidationError for bad LLM_PROVIDER must name all supported providers."""
        with pytest.raises(Exception) as exc_info:
            make_settings(LLM_PROVIDER="cohere")
        err = str(exc_info.value)
        # The validator message lists supported providers
        for name in ("openai", "anthropic", "gemini", "azure", "ollama"):
            assert name in err, f"Expected '{name}' in error message: {err}"

    def test_llm_generation_params_defaults(self) -> None:
        """Root Settings passes LLM generation defaults to LLMSettings.llm."""
        s = make_settings(LLM_PROVIDER="openai", LLM_DEFAULT_MODEL="gpt-4o-mini")
        llm = s.llm
        assert llm.temperature == 0.7
        assert llm.max_tokens == 2048
        assert llm.streaming is True

    def test_llm_generation_params_env_override(self) -> None:
        """LLM_TEMPERATURE, LLM_MAX_TOKENS, LLM_STREAMING can be overridden via env."""
        s = make_settings(
            LLM_PROVIDER="openai",
            LLM_DEFAULT_MODEL="gpt-4o",
            LLM_TEMPERATURE="0.2",
            LLM_MAX_TOKENS="512",
            LLM_STREAMING="false",
        )
        llm = s.llm
        assert llm.temperature == 0.2
        assert llm.max_tokens == 512
        assert llm.streaming is False

    def test_llm_generation_params_in_kwargs(self) -> None:
        """as_litellm_kwargs includes temperature, max_tokens, streaming."""
        s = make_settings(
            LLM_PROVIDER="openai",
            LLM_DEFAULT_MODEL="gpt-4o",
            OPENAI_API_KEY="sk-test",
            LLM_TEMPERATURE="0.5",
            LLM_MAX_TOKENS="1024",
        )
        kwargs = s.llm.as_litellm_kwargs()
        assert kwargs["temperature"] == 0.5
        assert kwargs["max_tokens"] == 1024
        assert "model" in kwargs
        assert "api_key" in kwargs

    def test_provider_switching_changes_litellm_model(self) -> None:
        """Demonstrate that env change is sufficient to switch providers."""
        for provider, model, expected_prefix in [
            ("openai", "gpt-4o", "openai/"),
            ("anthropic", "claude-3-5-haiku-20241022", "anthropic/"),
            ("gemini", "gemini-2.0-flash", "gemini/"),
            ("ollama", "mistral", "ollama/"),
        ]:
            s = make_settings(LLM_PROVIDER=provider, LLM_DEFAULT_MODEL=model)
            assert s.llm.litellm_model.startswith(expected_prefix), (
                f"Provider '{provider}' should produce model string starting with '{expected_prefix}'"
            )


# ---------------------------------------------------------------------------
# get_settings cache
# ---------------------------------------------------------------------------


class TestGetSettingsCache:
    def test_returns_same_instance(self) -> None:
        get_settings.cache_clear()
        a = get_settings()
        b = get_settings()
        assert a is b

    def test_cache_clear_allows_fresh_instance(self) -> None:
        get_settings.cache_clear()
        a = get_settings()
        get_settings.cache_clear()
        b = get_settings()
        assert type(a) is type(b)
        assert isinstance(b, Settings)
