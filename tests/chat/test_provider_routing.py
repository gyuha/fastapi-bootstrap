"""Unit tests: LLM provider factory/router routing (Sub-AC 14.3.2).

Verifies that ``LLM_PROVIDER=openai`` routes to the OpenAI endpoint and model,
and ``LLM_PROVIDER=ollama`` routes to the Ollama endpoint and model, **without
modifying any chat domain code**.

The full routing chain under test
----------------------------------
::

    LLM_PROVIDER env var
        → Settings / Settings.llm (LLMSettings)
        → ProviderFactory.make_model_string() / make_kwargs()
        → ChatLiteLLM(**kwargs)          ← patched; kwargs captured and asserted

Every test is a **pure unit test** — ``ChatLiteLLM`` is patched at the module
boundary, so no real network calls are made.  Only environment variables (and
directly-constructed ``LLMSettings`` objects) differ between OpenAI and Ollama
scenarios; the factory, client, and service code are **never modified**.

Covered scenarios
-----------------
OpenAI routing (``LLM_PROVIDER=openai``):
  * Model string starts with ``openai/``
  * ``ChatLiteLLM`` receives ``model="openai/<model>"``
  * ``ChatLiteLLM`` receives the real ``api_key`` (from ``OPENAI_API_KEY``)
  * ``ChatLiteLLM`` does **not** receive ``api_base`` (uses OpenAI's default)
  * ``get_llm_client()`` FastAPI dependency routes to OpenAI
  * ``ProviderFactory`` returns correct model string and kwargs

Ollama routing (``LLM_PROVIDER=ollama``):
  * Model string starts with ``ollama/``
  * ``ChatLiteLLM`` receives ``model="ollama/<model>"``
  * ``ChatLiteLLM`` receives ``api_base=<OLLAMA_BASE_URL>``
  * ``ChatLiteLLM`` receives ``api_key="ollama"`` (litellm sentinel, not a real key)
  * Custom ``OLLAMA_BASE_URL`` is forwarded verbatim to ``api_base``
  * ``get_llm_client()`` FastAPI dependency routes to Ollama
  * ``ProviderFactory`` returns correct model string and kwargs

Provider switching (env-only, zero code change):
  * Switching from OpenAI to Ollama changes model prefix, api_base presence, api_key
  * Both providers use identical factory + client code paths
  * All five supported providers produce the correct prefix via ``ProviderFactory``
  * env-based switching produces different ``ChatLiteLLM`` kwargs without code changes
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.config import LLMProvider, LLMSettings, get_settings
from domains.chat.llm_client import LLMClient, get_llm_client
from domains.chat.llm_factory import ProviderFactory

# ---------------------------------------------------------------------------
# Constants — fake credentials / endpoints (never hit a real provider)
# ---------------------------------------------------------------------------

OPENAI_TEST_KEY: str = "sk-test-openai-provider-routing-key"
OPENAI_TEST_MODEL: str = "gpt-4o-mini"

OLLAMA_TEST_URL: str = "http://localhost:11434"
OLLAMA_TEST_MODEL: str = "llama3.2"
OLLAMA_CUSTOM_URL: str = "http://custom-ollama:11434"
OLLAMA_CUSTOM_MODEL: str = "mistral"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_settings(
    model: str = OPENAI_TEST_MODEL,
    api_key: str = OPENAI_TEST_KEY,
) -> LLMSettings:
    """Construct an ``LLMSettings`` for OpenAI without touching the environment."""
    return LLMSettings(
        provider=LLMProvider.openai,
        default_model=model,
        OPENAI_API_KEY=api_key,
    )


def _make_ollama_settings(
    model: str = OLLAMA_TEST_MODEL,
    base_url: str = OLLAMA_TEST_URL,
) -> LLMSettings:
    """Construct an ``LLMSettings`` for Ollama without touching the environment."""
    return LLMSettings(
        provider=LLMProvider.ollama,
        default_model=model,
        OLLAMA_BASE_URL=base_url,
    )


def _capture_litellm_kwargs() -> tuple[dict[str, Any], MagicMock]:
    """Return ``(captured, mock_cls)`` for asserting on ``ChatLiteLLM`` constructor kwargs.

    Usage::

        captured, mock_cls = _capture_litellm_kwargs()
        with patch("...infra.llm.provider_factory.ChatLiteLLM", mock_cls):
            LLMClient(settings=my_settings)
        assert captured["model"] == "openai/gpt-4o-mini"
    """
    captured: dict[str, Any] = {}

    def _side_effect(**kw: Any) -> MagicMock:
        captured.update(kw)
        return MagicMock()

    mock_cls = MagicMock(side_effect=_side_effect)
    return captured, mock_cls


_CHAT_LITELLM_PATH = "infra.llm.provider_factory.ChatLiteLLM"
_GET_SETTINGS_PATH = "domains.chat.llm_client.get_settings"


# ---------------------------------------------------------------------------
# OpenAI provider routing — LLM_PROVIDER=openai
# ---------------------------------------------------------------------------


class TestOpenAIProviderRouting:
    """Verify that LLM_PROVIDER=openai routes to the OpenAI endpoint and model.

    All routing is determined by environment variables / LLMSettings alone.
    No chat domain code is modified between tests.
    """

    # ── Model string ─────────────────────────────────────────────────────────

    def test_model_string_starts_with_openai_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LLMSettings.litellm_model starts with 'openai/' when LLM_PROVIDER=openai."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_TEST_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)

        s = get_settings()
        assert s.llm.litellm_model.startswith("openai/"), (
            f"Expected litellm_model to start with 'openai/', got {s.llm.litellm_model!r}"
        )

    def test_model_string_contains_model_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Model name is preserved verbatim after the 'openai/' prefix."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_TEST_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)

        s = get_settings()
        assert s.llm.litellm_model == f"openai/{OPENAI_TEST_MODEL}"

    def test_provider_factory_openai_model_string(self) -> None:
        """ProviderFactory.make_model_string('openai', ...) returns 'openai/<model>'."""
        result = ProviderFactory.make_model_string("openai", OPENAI_TEST_MODEL)
        assert result == f"openai/{OPENAI_TEST_MODEL}"

    def test_provider_factory_enum_openai_model_string(self) -> None:
        """ProviderFactory accepts LLMProvider enum value for OpenAI."""
        result = ProviderFactory.make_model_string(LLMProvider.openai, OPENAI_TEST_MODEL)
        assert result == f"openai/{OPENAI_TEST_MODEL}"

    # ── ChatLiteLLM constructor kwargs ────────────────────────────────────────

    def test_chat_litellm_receives_openai_model_kwarg(self) -> None:
        """ChatLiteLLM is instantiated with model='openai/<model>' for OpenAI settings."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_openai_settings())

        assert captured["model"] == f"openai/{OPENAI_TEST_MODEL}", (
            f"Expected model='openai/{OPENAI_TEST_MODEL}', got {captured.get('model')!r}"
        )

    def test_chat_litellm_receives_openai_api_key(self) -> None:
        """ChatLiteLLM is instantiated with the real OpenAI API key."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_openai_settings(api_key=OPENAI_TEST_KEY))

        assert captured.get("api_key") == OPENAI_TEST_KEY, (
            f"Expected api_key={OPENAI_TEST_KEY!r}, got {captured.get('api_key')!r}"
        )

    def test_chat_litellm_no_api_base_for_openai(self) -> None:
        """OpenAI uses its default base URL — api_base must NOT be in kwargs."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_openai_settings())

        assert "api_base" not in captured, (
            f"'api_base' should not be passed for OpenAI, got {captured.get('api_base')!r}"
        )

    def test_chat_litellm_no_api_version_for_openai(self) -> None:
        """OpenAI does not use api_version — it is an Azure-only kwarg."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_openai_settings())

        assert "api_version" not in captured

    def test_provider_factory_openai_kwargs_complete(self) -> None:
        """ProviderFactory.make_kwargs() returns model + api_key for OpenAI (no api_base)."""
        kwargs = ProviderFactory.make_kwargs(_make_openai_settings())
        assert kwargs["model"] == f"openai/{OPENAI_TEST_MODEL}"
        assert kwargs["api_key"] == OPENAI_TEST_KEY
        assert "api_base" not in kwargs

    def test_llm_client_model_string_openai(self) -> None:
        """LLMClient.model_string is 'openai/<model>' for OpenAI settings."""
        with patch(_CHAT_LITELLM_PATH):
            client = LLMClient(settings=_make_openai_settings())
        assert client.model_string == f"openai/{OPENAI_TEST_MODEL}"

    def test_llm_client_provider_is_openai(self) -> None:
        """LLMClient.provider is 'openai' for OpenAI settings."""
        with patch(_CHAT_LITELLM_PATH):
            client = LLMClient(settings=_make_openai_settings())
        assert client.provider == "openai"

    # ── FastAPI dependency chain ──────────────────────────────────────────────

    def test_get_llm_client_routes_to_openai(self) -> None:
        """get_llm_client() FastAPI dependency returns an OpenAI-routed LLMClient."""
        openai_llm = _make_openai_settings()
        with (
            patch(_GET_SETTINGS_PATH) as mock_gs,
            patch(_CHAT_LITELLM_PATH),
        ):
            mock_gs.return_value.llm = openai_llm
            client = get_llm_client()

        assert client.model_string == f"openai/{OPENAI_TEST_MODEL}"
        assert client.provider == "openai"

    # ── Env-var chain ─────────────────────────────────────────────────────────

    def test_env_openai_routes_model_string_via_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end env→settings→model_string routing for OpenAI."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_TEST_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)

        s = get_settings()
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=s.llm)

        assert captured["model"] == f"openai/{OPENAI_TEST_MODEL}"
        assert captured.get("api_key") == OPENAI_TEST_KEY
        assert "api_base" not in captured

    def test_env_openai_custom_model_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Custom LLM_DEFAULT_MODEL is preserved verbatim in the 'openai/<model>' string."""
        custom_model = "gpt-4-turbo-preview"
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", custom_model)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)

        s = get_settings()
        assert s.llm.litellm_model == f"openai/{custom_model}"


# ---------------------------------------------------------------------------
# Ollama provider routing — LLM_PROVIDER=ollama
# ---------------------------------------------------------------------------


class TestOllamaProviderRouting:
    """Verify that LLM_PROVIDER=ollama routes to the local Ollama endpoint.

    Ollama routing has three distinctive properties:
    1. Model string prefix is 'ollama/'
    2. ``api_base`` is set to ``OLLAMA_BASE_URL`` (local server URL)
    3. ``api_key`` is the litellm sentinel string ``"ollama"`` (no real key)
    """

    # ── Model string ─────────────────────────────────────────────────────────

    def test_model_string_starts_with_ollama_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LLMSettings.litellm_model starts with 'ollama/' when LLM_PROVIDER=ollama."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_TEST_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)

        s = get_settings()
        assert s.llm.litellm_model.startswith("ollama/"), (
            f"Expected litellm_model to start with 'ollama/', got {s.llm.litellm_model!r}"
        )

    def test_model_string_contains_model_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Model name is preserved verbatim after the 'ollama/' prefix."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_TEST_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)

        s = get_settings()
        assert s.llm.litellm_model == f"ollama/{OLLAMA_TEST_MODEL}"

    def test_provider_factory_ollama_model_string(self) -> None:
        """ProviderFactory.make_model_string('ollama', ...) returns 'ollama/<model>'."""
        result = ProviderFactory.make_model_string("ollama", OLLAMA_TEST_MODEL)
        assert result == f"ollama/{OLLAMA_TEST_MODEL}"

    def test_provider_factory_enum_ollama_model_string(self) -> None:
        """ProviderFactory accepts LLMProvider enum value for Ollama."""
        result = ProviderFactory.make_model_string(LLMProvider.ollama, OLLAMA_TEST_MODEL)
        assert result == f"ollama/{OLLAMA_TEST_MODEL}"

    # ── ChatLiteLLM constructor kwargs ────────────────────────────────────────

    def test_chat_litellm_receives_ollama_model_kwarg(self) -> None:
        """ChatLiteLLM is instantiated with model='ollama/<model>' for Ollama settings."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_ollama_settings())

        assert captured["model"] == f"ollama/{OLLAMA_TEST_MODEL}", (
            f"Expected model='ollama/{OLLAMA_TEST_MODEL}', got {captured.get('model')!r}"
        )

    def test_chat_litellm_receives_ollama_api_base(self) -> None:
        """ChatLiteLLM receives api_base pointing to the Ollama server URL."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_ollama_settings(base_url=OLLAMA_TEST_URL))

        assert captured.get("api_base") == OLLAMA_TEST_URL, (
            f"Expected api_base={OLLAMA_TEST_URL!r}, got {captured.get('api_base')!r}"
        )

    def test_chat_litellm_receives_ollama_sentinel_api_key(self) -> None:
        """ChatLiteLLM receives api_key='ollama' — the litellm sentinel for local servers."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_ollama_settings())

        assert captured.get("api_key") == "ollama", (
            f"Expected api_key='ollama' (litellm sentinel), got {captured.get('api_key')!r}"
        )

    def test_chat_litellm_api_base_not_a_cloud_url(self) -> None:
        """Ollama api_base must be a local URL, not an OpenAI/cloud endpoint."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_ollama_settings(base_url=OLLAMA_TEST_URL))

        api_base = captured.get("api_base", "")
        assert "openai.com" not in api_base
        assert "anthropic.com" not in api_base
        assert "googleapis.com" not in api_base

    def test_custom_ollama_url_forwarded_as_api_base(self) -> None:
        """Custom OLLAMA_BASE_URL is forwarded verbatim as api_base."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_ollama_settings(base_url=OLLAMA_CUSTOM_URL))

        assert captured.get("api_base") == OLLAMA_CUSTOM_URL

    def test_custom_ollama_model_forwarded(self) -> None:
        """Custom model name (e.g. mistral) is forwarded correctly."""
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=_make_ollama_settings(model=OLLAMA_CUSTOM_MODEL))

        assert captured["model"] == f"ollama/{OLLAMA_CUSTOM_MODEL}"

    def test_provider_factory_ollama_kwargs_complete(self) -> None:
        """ProviderFactory.make_kwargs() returns model + api_base + sentinel key for Ollama."""
        kwargs = ProviderFactory.make_kwargs(_make_ollama_settings())
        assert kwargs["model"] == f"ollama/{OLLAMA_TEST_MODEL}"
        assert kwargs["api_base"] == OLLAMA_TEST_URL
        assert kwargs["api_key"] == "ollama"

    def test_llm_client_model_string_ollama(self) -> None:
        """LLMClient.model_string is 'ollama/<model>' for Ollama settings."""
        with patch(_CHAT_LITELLM_PATH):
            client = LLMClient(settings=_make_ollama_settings())
        assert client.model_string == f"ollama/{OLLAMA_TEST_MODEL}"

    def test_llm_client_provider_is_ollama(self) -> None:
        """LLMClient.provider is 'ollama' for Ollama settings."""
        with patch(_CHAT_LITELLM_PATH):
            client = LLMClient(settings=_make_ollama_settings())
        assert client.provider == "ollama"

    # ── FastAPI dependency chain ──────────────────────────────────────────────

    def test_get_llm_client_routes_to_ollama(self) -> None:
        """get_llm_client() FastAPI dependency returns an Ollama-routed LLMClient."""
        ollama_llm = _make_ollama_settings()
        with (
            patch(_GET_SETTINGS_PATH) as mock_gs,
            patch(_CHAT_LITELLM_PATH),
        ):
            mock_gs.return_value.llm = ollama_llm
            client = get_llm_client()

        assert client.model_string == f"ollama/{OLLAMA_TEST_MODEL}"
        assert client.provider == "ollama"

    # ── Env-var chain ─────────────────────────────────────────────────────────

    def test_env_ollama_routes_full_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end env→settings→ChatLiteLLM routing for Ollama."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_TEST_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)

        s = get_settings()
        captured, mock_cls = _capture_litellm_kwargs()
        with patch(_CHAT_LITELLM_PATH, mock_cls):
            LLMClient(settings=s.llm)

        assert captured["model"] == f"ollama/{OLLAMA_TEST_MODEL}"
        assert captured.get("api_base") == OLLAMA_TEST_URL
        assert captured.get("api_key") == "ollama"

    def test_env_ollama_tagged_model_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Model names with Ollama tags (e.g. 'codellama:13b') are preserved verbatim."""
        tagged_model = "codellama:13b"
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", tagged_model)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)

        s = get_settings()
        assert s.llm.litellm_model == f"ollama/{tagged_model}"

    def test_env_ollama_no_cloud_api_key_needed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ollama does not require OPENAI_API_KEY or any other cloud API key."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_TEST_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        s = get_settings()
        # Ollama active key must be empty (no real cloud key)
        assert s.llm.active_api_key == ""
        # but as_litellm_kwargs still includes the "ollama" sentinel
        kwargs = s.llm.as_litellm_kwargs()
        assert kwargs["api_key"] == "ollama"


# ---------------------------------------------------------------------------
# Provider switching — env-only change, zero chat domain code modification
# ---------------------------------------------------------------------------


class TestProviderSwitchingViaEnv:
    """Switching between OpenAI and Ollama requires only an environment variable change.

    This class documents the ``llm_provider_portability`` evaluation principle:
    the routing logic (factory + client) handles all provider-specific differences
    internally.  No chat domain code is modified when switching providers.
    """

    def test_openai_to_ollama_switch_changes_model_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changing LLM_PROVIDER=openai → ollama changes the model string prefix."""
        # Scenario 1: OpenAI
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_TEST_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
        get_settings.cache_clear()
        openai_model = get_settings().llm.litellm_model

        # Scenario 2: Ollama — env-only change, no code modification
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_TEST_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        get_settings.cache_clear()
        ollama_model = get_settings().llm.litellm_model

        assert openai_model.startswith("openai/")
        assert ollama_model.startswith("ollama/")
        assert openai_model != ollama_model

    def test_provider_switch_changes_api_base_presence(self) -> None:
        """OpenAI has no api_base; Ollama always provides api_base."""
        openai_kwargs = ProviderFactory.make_kwargs(_make_openai_settings())
        ollama_kwargs = ProviderFactory.make_kwargs(_make_ollama_settings())

        # OpenAI must NOT have api_base (uses its default endpoint)
        assert "api_base" not in openai_kwargs, "OpenAI should not have api_base in kwargs"
        # Ollama must have api_base (local server URL)
        assert "api_base" in ollama_kwargs, "Ollama must have api_base in kwargs"
        assert ollama_kwargs["api_base"] == OLLAMA_TEST_URL

    def test_provider_switch_changes_api_key_semantics(self) -> None:
        """OpenAI key is a real credential; Ollama uses the 'ollama' sentinel."""
        openai_kwargs = ProviderFactory.make_kwargs(_make_openai_settings())
        ollama_kwargs = ProviderFactory.make_kwargs(_make_ollama_settings())

        assert openai_kwargs["api_key"] == OPENAI_TEST_KEY
        assert ollama_kwargs["api_key"] == "ollama"  # litellm sentinel, not a real key
        assert openai_kwargs["api_key"] != ollama_kwargs["api_key"]

    def test_env_switch_produces_different_chat_litellm_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env-based switching produces different ChatLiteLLM kwargs without code changes.

        This is the definitive zero-code-change test: identical client construction
        code (``LLMClient(settings=s.llm)``) produces correctly differentiated
        ``ChatLiteLLM`` kwargs when only the environment variables differ.
        """
        openai_captured: dict[str, Any] = {}
        ollama_captured: dict[str, Any] = {}

        def _capture_openai(**kw: Any) -> MagicMock:
            openai_captured.update(kw)
            return MagicMock()

        def _capture_ollama(**kw: Any) -> MagicMock:
            ollama_captured.update(kw)
            return MagicMock()

        # ── OpenAI scenario ──────────────────────────────────────────────────
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_TEST_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
        get_settings.cache_clear()

        with patch(_CHAT_LITELLM_PATH, side_effect=_capture_openai):
            # ← identical code path; only settings differ
            LLMClient(settings=get_settings().llm)

        # ── Ollama scenario (env-only change) ────────────────────────────────
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_TEST_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        get_settings.cache_clear()

        with patch(_CHAT_LITELLM_PATH, side_effect=_capture_ollama):
            # ← identical code path; only settings differ
            LLMClient(settings=get_settings().llm)

        # ── Assert routing diverged correctly ─────────────────────────────────
        assert openai_captured["model"].startswith("openai/")
        assert ollama_captured["model"].startswith("ollama/")
        assert openai_captured["model"] != ollama_captured["model"]
        assert openai_captured.get("api_key") == OPENAI_TEST_KEY
        assert ollama_captured.get("api_key") == "ollama"
        assert "api_base" not in openai_captured
        assert "api_base" in ollama_captured
        assert ollama_captured["api_base"] == OLLAMA_TEST_URL

    def test_same_client_class_used_for_both_providers(self) -> None:
        """LLMClient is the same class for OpenAI and Ollama — routing is data-driven."""
        with patch(_CHAT_LITELLM_PATH):
            openai_client = LLMClient(settings=_make_openai_settings())
            ollama_client = LLMClient(settings=_make_ollama_settings())

        # Same class, different routing — zero code change
        assert type(openai_client) is type(ollama_client)
        assert openai_client.model_string != ollama_client.model_string
        assert openai_client.provider == "openai"
        assert ollama_client.provider == "ollama"

    def test_same_factory_method_used_for_all_providers(self) -> None:
        """ProviderFactory.make_model_string is the same method for every provider.

        Documents the zero-code-change guarantee: the factory method does not
        branch at call sites — it is called identically for every provider.
        """
        # All providers use the exact same factory method (no per-provider call sites)
        openai_str = ProviderFactory.make_model_string("openai", OPENAI_TEST_MODEL)
        ollama_str = ProviderFactory.make_model_string("ollama", OLLAMA_TEST_MODEL)

        assert openai_str == f"openai/{OPENAI_TEST_MODEL}"
        assert ollama_str == f"ollama/{OLLAMA_TEST_MODEL}"
        assert openai_str != ollama_str  # different outputs, same code path

    @pytest.mark.parametrize(
        ("provider", "model", "expected_prefix", "expect_api_base"),
        [
            ("openai", "gpt-4o", "openai/", False),
            ("anthropic", "claude-3-5-sonnet-20241022", "anthropic/", False),
            ("gemini", "gemini-1.5-flash", "gemini/", False),
            ("ollama", "llama3.2", "ollama/", True),
        ],
    )
    def test_all_standard_providers_route_correctly(
        self,
        provider: str,
        model: str,
        expected_prefix: str,
        expect_api_base: bool,
    ) -> None:
        """All standard (non-Azure) providers produce the correct model string prefix.

        This parametrized test verifies the data-driven routing guarantee:
        every supported provider produces a correctly-prefixed model string
        using the exact same ``ProviderFactory.make_model_string`` call.
        """
        model_str = ProviderFactory.make_model_string(provider, model)
        assert model_str.startswith(expected_prefix), (
            f"Provider '{provider}' should produce string starting with "
            f"'{expected_prefix}', got {model_str!r}"
        )
        # Model name is always preserved verbatim
        assert model_str.split("/", 1)[1] == model

    @pytest.mark.parametrize(
        ("provider", "model", "expected_prefix"),
        [
            ("openai", "gpt-4o", "openai/"),
            ("anthropic", "claude-3-5-haiku-20241022", "anthropic/"),
            ("gemini", "gemini-2.0-flash", "gemini/"),
            ("ollama", "mistral", "ollama/"),
        ],
    )
    def test_provider_prefix_lowercase_for_litellm(
        self, provider: str, model: str, expected_prefix: str
    ) -> None:
        """Provider prefix in model string is always lowercase (litellm requirement)."""
        model_str = ProviderFactory.make_model_string(provider, model)
        prefix = model_str.split("/")[0]
        assert prefix == prefix.lower(), (
            f"Provider prefix '{prefix}' must be lowercase for litellm compatibility"
        )
        assert model_str.startswith(expected_prefix)


# ---------------------------------------------------------------------------
# Routing specificity — OpenAI vs Ollama kwargs differ in exactly the right ways
# ---------------------------------------------------------------------------


class TestRoutingDifferentiators:
    """Verify the exact kwargs differences between OpenAI and Ollama routing.

    Documents the contract that makes provider switching transparent:
    the factory produces *exactly* the kwargs litellm needs for each provider,
    nothing more, nothing less.
    """

    def test_openai_kwargs_contain_model_and_api_key_only(self) -> None:
        """OpenAI kwargs contain 'model' and 'api_key' — no extra provider-specific keys."""
        kwargs = ProviderFactory.make_kwargs(_make_openai_settings())
        assert "model" in kwargs
        assert "api_key" in kwargs
        # OpenAI must not have Azure/Ollama-specific keys
        assert "api_base" not in kwargs
        assert "api_version" not in kwargs

    def test_ollama_kwargs_contain_model_api_base_and_sentinel_key(self) -> None:
        """Ollama kwargs contain 'model', 'api_base', and 'api_key'='ollama'."""
        kwargs = ProviderFactory.make_kwargs(_make_ollama_settings())
        assert "model" in kwargs
        assert "api_base" in kwargs
        assert "api_key" in kwargs
        assert kwargs["api_key"] == "ollama"

    def test_ollama_api_key_is_sentinel_not_empty(self) -> None:
        """Ollama api_key must be the 'ollama' sentinel, not an empty string.

        litellm distinguishes between 'ollama' (sentinel → no auth) and
        empty string (missing key → may cause auth errors).
        """
        kwargs = ProviderFactory.make_kwargs(_make_ollama_settings())
        assert kwargs["api_key"] == "ollama"
        assert kwargs["api_key"] != ""
        assert kwargs["api_key"] is not None

    def test_openai_api_key_excluded_when_empty(self) -> None:
        """When OPENAI_API_KEY is not set, api_key is excluded from kwargs.

        This allows litellm to fall back to reading the key from its own
        environment variables (OPENAI_API_KEY), avoiding a redundant empty string.
        """
        settings_no_key = LLMSettings(
            provider=LLMProvider.openai,
            default_model=OPENAI_TEST_MODEL,
            OPENAI_API_KEY="",
        )
        kwargs = ProviderFactory.make_kwargs(settings_no_key)
        assert kwargs["model"] == f"openai/{OPENAI_TEST_MODEL}"
        assert "api_key" not in kwargs, (
            "Empty OpenAI API key should be excluded from kwargs (litellm reads from env instead)"
        )

    def test_model_strings_differ_and_are_provider_prefixed(self) -> None:
        """OpenAI and Ollama model strings differ and carry the correct provider prefix."""
        openai_settings = _make_openai_settings()
        ollama_settings = _make_ollama_settings()

        openai_model = ProviderFactory.from_settings(openai_settings)
        ollama_model = ProviderFactory.from_settings(ollama_settings)

        assert openai_model != ollama_model
        assert openai_model.startswith("openai/")
        assert ollama_model.startswith("ollama/")

    def test_from_settings_matches_make_model_string_for_openai(self) -> None:
        """from_settings and make_model_string return identical results for OpenAI."""
        settings = _make_openai_settings()
        via_settings = ProviderFactory.from_settings(settings)
        via_direct = ProviderFactory.make_model_string("openai", OPENAI_TEST_MODEL)
        assert via_settings == via_direct

    def test_from_settings_matches_make_model_string_for_ollama(self) -> None:
        """from_settings and make_model_string return identical results for Ollama."""
        settings = _make_ollama_settings()
        via_settings = ProviderFactory.from_settings(settings)
        via_direct = ProviderFactory.make_model_string("ollama", OLLAMA_TEST_MODEL)
        assert via_settings == via_direct
