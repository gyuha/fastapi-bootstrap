"""Application configuration via Pydantic Settings.

All settings are loaded from environment variables (or .env file).
Grouped to match the sections in .env.example.

Usage::

    from {{ cookiecutter.package_name }}.core.config import settings

    db_url = settings.async_database_url
{% if cookiecutter.include_chat_domain == "yes" %}
    llm_kwargs = settings.llm.as_litellm_kwargs()
{% endif %}
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AppEnv(str, Enum):
    development = "development"
    staging = "staging"
    production = "production"


{% if cookiecutter.include_chat_domain == "yes" %}
class LLMProvider(str, Enum):
    """Supported LLM providers.

    The value is used as the litellm provider prefix, e.g. ``openai/gpt-4o``.
    Switching providers requires only changing ``LLM_PROVIDER`` in .env.
    """

    openai = "openai"
    anthropic = "anthropic"
    gemini = "gemini"
    azure = "azure"
    ollama = "ollama"

{% endif %}

class LogFormat(str, Enum):
    json = "json"
    console = "console"


{% if cookiecutter.include_chat_domain == "yes" %}
# ---------------------------------------------------------------------------
# LLM settings (standalone — can be used independently of root Settings)
# ---------------------------------------------------------------------------


class LLMSettings(BaseSettings):
    """LLM provider settings for the chat domain.

    Supported providers: openai, anthropic, gemini, azure, ollama.
    Only credentials for the *active* provider need to be configured.

    The ``litellm_model`` property returns the full litellm model identifier
    ready to pass to ``ChatLiteLLM(model=...)``.

    Examples::

        # Pure env-var usage (reads LLM_PROVIDER, LLM_DEFAULT_MODEL, etc.)
        from {{ cookiecutter.package_name }}.core.config import LLMSettings
        llm = LLMSettings()

        # Via root settings accessor
        from {{ cookiecutter.package_name }}.core.config import settings
        llm = settings.llm
        model_str = llm.litellm_model      # "openai/gpt-4o-mini"
        kwargs    = llm.as_litellm_kwargs() # {model: ..., api_key: ...}
    """

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        populate_by_name=True,
        extra="ignore",
    )

    # ── Core ──────────────────────────────────────────────────────────────────
    # env: LLM_PROVIDER
    provider: LLMProvider = LLMProvider("{{ cookiecutter.llm_provider }}")

    # env: LLM_DEFAULT_MODEL
    default_model: str = "{{ cookiecutter.llm_default_model }}"

    # ── Per-provider credentials (no LLM_ prefix — use field aliases) ─────────
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="OPENAI_API_KEY",
        description="OpenAI API key (sk-...).",
    )
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="ANTHROPIC_API_KEY",
        description="Anthropic API key (sk-ant-...).",
    )
    gemini_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="GEMINI_API_KEY",
        description="Google Gemini API key.",
    )

    # Azure OpenAI
    azure_openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="AZURE_OPENAI_API_KEY",
    )
    azure_openai_endpoint: str = Field(default="", alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_deployment: str = Field(default="", alias="AZURE_OPENAI_DEPLOYMENT")
    azure_openai_api_version: str = Field(
        default="2024-08-01-preview",
        alias="AZURE_OPENAI_API_VERSION",
    )

    # Ollama (local — no API key)
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        alias="OLLAMA_BASE_URL",
    )

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def litellm_model(self) -> str:
        """Full litellm model identifier for the active provider.

        Format: ``<provider>/<model>``

        Examples:
            ``openai/gpt-4o-mini``
            ``anthropic/claude-3-5-sonnet-20241022``
            ``gemini/gemini-1.5-flash``
            ``azure/my-deployment``
            ``ollama/llama3.2``
        """
        if self.provider == LLMProvider.azure:
            deployment = self.azure_openai_deployment or self.default_model
            return f"azure/{deployment}"
        if self.provider == LLMProvider.gemini:
            return f"gemini/{self.default_model}"
        return f"{self.provider.value}/{self.default_model}"

    @property
    def active_api_key(self) -> str:
        """Return the API key for the active provider as a plain string.

        Returns an empty string for Ollama (no key needed).
        """
        _map: dict[LLMProvider, SecretStr] = {
            LLMProvider.openai: self.openai_api_key,
            LLMProvider.anthropic: self.anthropic_api_key,
            LLMProvider.gemini: self.gemini_api_key,
            LLMProvider.azure: self.azure_openai_api_key,
            LLMProvider.ollama: SecretStr(""),
        }
        return _map[self.provider].get_secret_value()

    def as_litellm_kwargs(self) -> dict[str, Any]:
        """Return kwargs dict suitable for ``ChatLiteLLM(**kwargs)``.

        Includes ``model``, ``api_key``, and provider-specific params
        (``api_base`` for Ollama/Azure, ``api_version`` for Azure).
        Provider switching is transparent — change ``LLM_PROVIDER`` in .env.
        """
        kwargs: dict[str, Any] = {"model": self.litellm_model}

        if self.provider == LLMProvider.azure:
            kwargs["api_key"] = self.azure_openai_api_key.get_secret_value()
            kwargs["api_base"] = self.azure_openai_endpoint
            kwargs["api_version"] = self.azure_openai_api_version

        elif self.provider == LLMProvider.ollama:
            kwargs["api_base"] = self.ollama_base_url
            # litellm accepts "ollama" as a sentinel when no key is needed
            kwargs["api_key"] = "ollama"

        else:
            key = self.active_api_key
            if key:
                kwargs["api_key"] = key

        return kwargs


{% endif %}

# ---------------------------------------------------------------------------
# Root Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Top-level application settings.

    Reads from environment variables and, optionally, a ``.env`` file
    in the current working directory.

    All sub-sections (DB, Redis, JWT, OAuth, email, LLM) are flat fields
    on this model for simplicity.  The ``async_database_url``, ``redis_dsn``,
    and ``llm`` computed properties provide convenient derived values.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Application ───────────────────────────────────────────────────────────
    app_env: AppEnv = AppEnv.development
    app_debug: bool = False
    secret_key: SecretStr = SecretStr("change-me-in-production")
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:{{ cookiecutter.fastapi_port }}"],
        description="Allowed CORS origins. Accepts a list or a comma-separated string.",
    )

    # ── Server ────────────────────────────────────────────────────────────────
    host: str = "{{ cookiecutter.fastapi_host }}"
    port: int = {{ cookiecutter.fastapi_port }}
    workers: int = 1

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="",
        description="Full async PostgreSQL DSN. Auto-built from POSTGRES_* vars if empty.",
    )
    database_url_sync: str = Field(
        default="",
        description="Sync DSN for Alembic. Auto-built from POSTGRES_* vars if empty.",
    )
    postgres_host: str = "{{ cookiecutter.postgres_host }}"
    postgres_port: int = {{ cookiecutter.postgres_port }}
    postgres_user: str = "{{ cookiecutter.postgres_user }}"
    postgres_password: SecretStr = SecretStr("{{ cookiecutter.postgres_password }}")
    postgres_db: str = "{{ cookiecutter.postgres_db }}"

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = Field(
        default="",
        description="Full Redis DSN. Auto-built from REDIS_* vars if empty.",
    )
    redis_host: str = "{{ cookiecutter.redis_host }}"
    redis_port: int = {{ cookiecutter.redis_port }}
    redis_db: int = {{ cookiecutter.redis_db }}

    # ── JWT ───────────────────────────────────────────────────────────────────
    jwt_secret_key: SecretStr = SecretStr("change-me-jwt-secret-key")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = {{ cookiecutter.jwt_access_ttl_minutes }}
    jwt_refresh_token_expire_days: int = {{ cookiecutter.jwt_refresh_ttl_days }}

    # ── OAuth ─────────────────────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_redirect_uri: str = ""

    kakao_client_id: str = ""
    kakao_client_secret: SecretStr = SecretStr("")
    kakao_redirect_uri: str = ""

    naver_client_id: str = ""
    naver_client_secret: SecretStr = SecretStr("")
    naver_redirect_uri: str = ""

    # ── Email ─────────────────────────────────────────────────────────────────
    mail_server: str = "localhost"
    mail_port: int = {{ cookiecutter.mailpit_smtp_port }}
    mail_username: str = ""
    mail_password: SecretStr = SecretStr("")
    mail_from: str = "noreply@{{ cookiecutter.project_slug }}.local"
    mail_from_name: str = "{{ cookiecutter.project_name }}"
    mail_starttls: bool = False
    mail_ssl_tls: bool = False

{% if cookiecutter.include_chat_domain == "yes" %}
    # ── LLM / Chat domain ─────────────────────────────────────────────────────
    # env: LLM_PROVIDER
    llm_provider: LLMProvider = LLMProvider("{{ cookiecutter.llm_provider }}")
    # env: LLM_DEFAULT_MODEL
    llm_default_model: str = "{{ cookiecutter.llm_default_model }}"

    # Provider credentials — set only the key for the active provider
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")

    azure_openai_api_key: SecretStr = SecretStr("")
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-08-01-preview"

    ollama_base_url: str = "http://localhost:11434"

{% endif %}
    # ── Observability ─────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.json

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: Any) -> list[str]:
        """Accept a comma-separated string from env or a list."""
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v  # type: ignore[return-value]

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> str:
        return str(v).upper()

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def async_database_url(self) -> str:
        """Async SQLAlchemy DSN (postgresql+asyncpg://).

        Uses ``DATABASE_URL`` env var if set; otherwise builds from parts.
        """
        if self.database_url:
            return self.database_url
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_database_url(self) -> str:
        """Sync SQLAlchemy DSN for Alembic (postgresql+psycopg2://).

        Uses ``DATABASE_URL_SYNC`` env var if set; otherwise builds from parts.
        """
        if self.database_url_sync:
            return self.database_url_sync
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_dsn(self) -> str:
        """Full Redis DSN.

        Uses ``REDIS_URL`` env var if set; otherwise builds from parts.
        """
        return self.redis_url or f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

{% if cookiecutter.include_chat_domain == "yes" %}
    @property
    def llm(self) -> LLMSettings:
        """Return a fully-populated :class:`LLMSettings` for the chat domain.

        Reflects all LLM-related env vars already loaded by this
        :class:`Settings` instance.  Switching providers requires only
        changing ``LLM_PROVIDER`` (and the corresponding API key) in .env.

        Example::

            model_str = settings.llm.litellm_model      # "openai/gpt-4o-mini"
            kwargs    = settings.llm.as_litellm_kwargs() # {model: ..., api_key: ...}
        """
        return LLMSettings(
            provider=self.llm_provider,
            default_model=self.llm_default_model,
            # Aliases are accepted by populate_by_name=True
            OPENAI_API_KEY=self.openai_api_key,
            ANTHROPIC_API_KEY=self.anthropic_api_key,
            GEMINI_API_KEY=self.gemini_api_key,
            AZURE_OPENAI_API_KEY=self.azure_openai_api_key,
            AZURE_OPENAI_ENDPOINT=self.azure_openai_endpoint,
            AZURE_OPENAI_DEPLOYMENT=self.azure_openai_deployment,
            AZURE_OPENAI_API_VERSION=self.azure_openai_api_version,
            OLLAMA_BASE_URL=self.ollama_base_url,
        )

{% endif %}
    def is_production(self) -> bool:
        """Return True if running in production environment."""
        return self.app_env == AppEnv.production

    def is_development(self) -> bool:
        """Return True if running in development environment."""
        return self.app_env == AppEnv.development


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    Using ``lru_cache`` ensures the .env file is parsed exactly once per
    process.  In tests, call ``get_settings.cache_clear()`` before
    overriding env vars to force a fresh read.

    Example::

        from {{ cookiecutter.package_name }}.core.config import get_settings
        get_settings.cache_clear()  # in test teardown / fixtures
    """
    return Settings()


#: Module-level singleton — import and use directly in application code.
settings: Settings = get_settings()
