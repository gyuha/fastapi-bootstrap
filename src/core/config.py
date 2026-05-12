"""Application configuration via Pydantic Settings.

All settings are loaded from environment variables (or .env file).
Grouped to match the sections in .env.example.

Usage::

    from core.config import settings

    db_url = settings.async_database_url

    llm_kwargs = settings.llm.as_litellm_kwargs()

"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AppEnv(StrEnum):
    development = "development"
    staging = "staging"
    production = "production"


class LLMProvider(StrEnum):
    """Supported LLM providers.

    The value is used as the litellm provider prefix, e.g. ``openai/gpt-4o``.
    Switching providers requires only changing ``LLM_PROVIDER`` in .env.

    Supported values
    ----------------
    * ``openai``    — OpenAI (GPT-4o, GPT-4o-mini, …)
    * ``anthropic`` — Anthropic (Claude 3.5 Sonnet, Claude 3.5 Haiku, …)
    * ``gemini``    — Google Gemini (gemini-1.5-flash, gemini-2.0-flash, …)
    * ``azure``     — Azure OpenAI (uses deployment name instead of model)
    * ``ollama``    — Local Ollama inference server (no API key required)
    """

    openai = "openai"
    anthropic = "anthropic"
    gemini = "gemini"
    azure = "azure"
    ollama = "ollama"


class LogFormat(StrEnum):
    json = "json"
    console = "console"


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
        from core.config import LLMSettings
        llm = LLMSettings()

        # Via root settings accessor
        from core.config import settings
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
    # env: LLM_PROVIDER  (required — must be one of the LLMProvider enum values)
    provider: LLMProvider = LLMProvider("openai")

    # env: LLM_DEFAULT_MODEL
    default_model: str = "gpt-4o-mini"

    # ── Generation parameters ─────────────────────────────────────────────────
    # env: LLM_TEMPERATURE  (0.0 = deterministic, 2.0 = maximum creativity)
    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="Sampling temperature. 0.0 is deterministic; 2.0 is highly creative.",
    )

    # env: LLM_MAX_TOKENS
    max_tokens: int = Field(
        default=2048,
        gt=0,
        description="Maximum number of tokens to generate in the response.",
    )

    # env: LLM_STREAMING  (set to false to disable SSE streaming globally)
    streaming: bool = Field(
        default=True,
        description="Enable streaming responses via SSE by default.",
    )

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

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("provider", mode="before")
    @classmethod
    def _validate_provider(cls, v: Any) -> Any:
        """Validate LLM_PROVIDER value and emit a clear error for unsupported names.

        Pydantic's Enum coercion already catches invalid values, but this
        validator surfaces all supported provider names in the error message
        so that mis-configured environments are easy to diagnose.

        Example error::

            ValueError: LLM_PROVIDER='gpt4' is not supported.
            Supported providers: anthropic, azure, gemini, ollama, openai.
            Set LLM_PROVIDER in .env to one of the listed values.
        """
        valid = sorted(p.value for p in LLMProvider)
        if isinstance(v, str) and v not in valid:
            raise ValueError(
                f"LLM_PROVIDER={v!r} is not supported. "
                f"Supported providers: {', '.join(valid)}. "
                "Set LLM_PROVIDER in .env to one of the listed values."
            )
        return v

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

        Includes ``model``, generation parameters (``temperature``,
        ``max_tokens``, ``streaming``), ``api_key``, and provider-specific
        params (``api_base`` for Ollama/Azure, ``api_version`` for Azure).

        Provider switching is transparent — change ``LLM_PROVIDER`` in .env.

        The generation parameters (``LLM_TEMPERATURE``, ``LLM_MAX_TOKENS``,
        ``LLM_STREAMING``) can be overridden globally via environment variables
        or per-request by passing override kwargs to :class:`LLMClient`.
        """
        kwargs: dict[str, Any] = {
            "model": self.litellm_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "streaming": self.streaming,
        }

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
    app_env: AppEnv = AppEnv("development")
    app_debug: bool = False
    secret_key: SecretStr = SecretStr("change-me-in-production")
    # cors_origins stores raw value from env (str for comma-sep, list parsed from JSON).
    # The cors_origins_list property returns the final list[str].
    # Using str type avoids pydantic-settings v2 auto-JSON-decoding complex types.
    cors_origins: str = Field(
        default='["http://localhost:3000","http://localhost:8000"]',
        description="Allowed CORS origins. JSON array or comma-separated string.",
    )

    # ── Server ────────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
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
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "app"
    postgres_password: SecretStr = SecretStr("app")
    postgres_db: str = "app_db"

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = Field(
        default="",
        description="Full Redis DSN. Auto-built from REDIS_* vars if empty.",
    )
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # ── JWT ───────────────────────────────────────────────────────────────────
    jwt_secret_key: SecretStr = SecretStr("change-me-jwt-secret-key")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

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
    mail_port: int = 1025
    mail_username: str = ""
    mail_password: SecretStr = SecretStr("")
    mail_from: str = "noreply@fastapi-bootstrap.example.com"
    mail_from_name: str = "FastAPI Bootstrap"
    mail_starttls: bool = False
    mail_ssl_tls: bool = False

    # ── LLM / Chat domain ─────────────────────────────────────────────────────
    # env: LLM_PROVIDER  Supported: openai | anthropic | gemini | azure | ollama
    llm_provider: LLMProvider = LLMProvider("openai")
    # env: LLM_DEFAULT_MODEL
    llm_default_model: str = "gpt-4o-mini"

    # env: LLM_TEMPERATURE  (0.0 = deterministic, 2.0 = maximum creativity)
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # env: LLM_MAX_TOKENS
    llm_max_tokens: int = Field(default=2048, gt=0)
    # env: LLM_STREAMING  (false to disable global SSE streaming)
    llm_streaming: bool = True

    # Provider credentials — set only the key for the active provider
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")

    azure_openai_api_key: SecretStr = SecretStr("")
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-08-01-preview"

    ollama_base_url: str = "http://localhost:11434"

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _validate_llm_provider(cls, v: Any) -> Any:
        """Validate LLM_PROVIDER in root Settings with a helpful error message."""
        valid = sorted(p.value for p in LLMProvider)
        if isinstance(v, str) and v not in valid:
            raise ValueError(
                f"LLM_PROVIDER={v!r} is not supported. "
                f"Supported providers: {', '.join(valid)}. "
                "Set LLM_PROVIDER in .env to one of the listed values."
            )
        return v

    # ── Frontend ──────────────────────────────────────────────────────────────
    frontend_url: str = "http://localhost:3000"
    frontend_reset_confirm_url_base: str = "http://localhost:3000/auth/reset-confirm"

    # ── Observability ─────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat("json")

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> str:
        return str(v).upper()

    @field_validator("frontend_reset_confirm_url_base")
    @classmethod
    def _normalize_frontend_reset_confirm_url_base(cls, v: str) -> str:
        """Normalize reset-confirm link base before appending reset tokens."""
        return v.rstrip("/")

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse cors_origins string into a list.

        Accepts:
        - JSON array: ``["http://localhost:3000","http://localhost:8000"]``
        - Comma-separated: ``http://localhost:3000,http://localhost:8000``
        """
        import json as _json

        v = self.cors_origins.strip()
        if v.startswith("["):
            try:
                result = _json.loads(v)
                if isinstance(result, list):
                    return [str(o) for o in result]
            except _json.JSONDecodeError:
                pass
        return [o.strip() for o in v.split(",") if o.strip()]

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

    @property
    def mail_connection_config(self) -> dict[str, Any]:
        """Return keyword arguments suitable for ``fastapi_mail.ConnectionConfig(**kwargs)``.

        Assembles the email connection configuration from flat settings fields.
        Works with both the local Mailpit dev server and production SMTP providers.

        Local dev (default)::

            MAIL_SERVER=localhost
            MAIL_PORT=1025     # Mailpit SMTP
            MAIL_STARTTLS=false
            MAIL_SSL_TLS=false

        Production (SMTP provider rendered from cookiecutter)::

            MAIL_SERVER=smtp.fastapi-bootstrap.local
            MAIL_PORT=587
            MAIL_USERNAME=apikey
            MAIL_PASSWORD=SG.xxx
            MAIL_STARTTLS=true
            MAIL_SSL_TLS=false

        Local development runs FastAPI on the host, so the default
        ``MAIL_SERVER=localhost`` reaches the Mailpit SMTP port exposed by
        ``docker-compose.yml``. Production container SMTP values are injected
        by ``docker-compose.prod.yml`` / ``.env.prod``.

        Usage::

            from fastapi_mail import ConnectionConfig, FastMail
            from core.config import settings

            mail_config = ConnectionConfig(**settings.mail_connection_config)
            mailer = FastMail(mail_config)
        """
        return {
            "MAIL_USERNAME": self.mail_username,
            "MAIL_PASSWORD": self.mail_password.get_secret_value(),
            "MAIL_FROM": self.mail_from,
            "MAIL_PORT": self.mail_port,
            "MAIL_SERVER": self.mail_server,
            "MAIL_FROM_NAME": self.mail_from_name,
            "MAIL_STARTTLS": self.mail_starttls,
            "MAIL_SSL_TLS": self.mail_ssl_tls,
            # Credentials are only required when a username is set.
            # Mailpit accepts anonymous SMTP, so USE_CREDENTIALS=False in dev.
            "USE_CREDENTIALS": bool(self.mail_username),
            # Validate TLS certs only when TLS is actually enabled.
            "VALIDATE_CERTS": self.mail_ssl_tls or self.mail_starttls,
        }

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
            temperature=self.llm_temperature,
            max_tokens=self.llm_max_tokens,
            streaming=self.llm_streaming,
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

        from core.config import get_settings
        get_settings.cache_clear()  # in test teardown / fixtures
    """
    return Settings()


#: Module-level singleton — import and use directly in application code.
settings: Settings = get_settings()
