"""Tests for the LangChain-LiteLLM infra adapter factory.

Verifies that :func:`make_chat_litellm` in
:mod:`fastapi_bootstrap.infra.llm.provider_factory`:

* Returns a :class:`ChatLiteLLM` instance configured from the active provider.
* Passes the correct ``model``, ``api_key``, ``api_base``, and ``api_version``
  kwargs to :class:`ChatLiteLLM` for every supported provider.
* Merges ``override_kwargs`` after provider-derived kwargs.
* Falls back to the global settings singleton when ``settings`` is *None*.
* Lives in the *infra* layer — never imports domain code.

All tests are pure unit tests: ``ChatLiteLLM`` is patched at the module
boundary so no real network calls are made.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from core.config import LLMProvider, LLMSettings
from infra.llm.provider_factory import make_chat_litellm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_FACTORY_PATH = "infra.llm.provider_factory.ChatLiteLLM"

OPENAI_TEST_KEY = "sk-test-infra-factory-key"
OPENAI_TEST_MODEL = "gpt-4o-mini"

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_TEST_MODEL = "llama3.2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    provider: str = "openai",
    model: str = OPENAI_TEST_MODEL,
    *,
    openai_api_key: str = OPENAI_TEST_KEY,
    anthropic_api_key: str = "",
    gemini_api_key: str = "",
    azure_api_key: str = "",
    azure_endpoint: str = "",
    azure_deployment: str = "",
    azure_api_version: str = "2024-08-01-preview",
    ollama_base_url: str = OLLAMA_BASE_URL,
) -> LLMSettings:
    """Construct LLMSettings without touching the environment."""
    return LLMSettings(
        provider=LLMProvider(provider),
        default_model=model,
        OPENAI_API_KEY=openai_api_key,
        ANTHROPIC_API_KEY=anthropic_api_key,
        GEMINI_API_KEY=gemini_api_key,
        AZURE_OPENAI_API_KEY=azure_api_key,
        AZURE_OPENAI_ENDPOINT=azure_endpoint,
        AZURE_OPENAI_DEPLOYMENT=azure_deployment,
        AZURE_OPENAI_API_VERSION=azure_api_version,
        OLLAMA_BASE_URL=ollama_base_url,
    )


def _capture_kwargs() -> tuple[dict[str, Any], MagicMock]:
    """Return (captured_dict, side_effect_fn) for patching ChatLiteLLM.

    Usage::

        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(settings)
        assert captured["model"] == "openai/gpt-4o-mini"
    """
    captured: dict[str, Any] = {}

    def _side_effect(**kw: Any) -> MagicMock:
        captured.update(kw)
        return MagicMock()

    return captured, MagicMock(side_effect=_side_effect)


# ---------------------------------------------------------------------------
# Return type and basic behaviour
# ---------------------------------------------------------------------------


class TestMakeChatLiteLLMReturnType:
    """make_chat_litellm must return a ChatLiteLLM (or mock thereof) instance."""

    def test_returns_something(self) -> None:
        """make_chat_litellm must return a non-None object."""
        with patch(_PROVIDER_FACTORY_PATH) as mock_cls:
            result = make_chat_litellm(_make_settings("openai"))

        assert result is not None
        mock_cls.assert_called_once()

    def test_calls_chat_litellm_constructor(self) -> None:
        """make_chat_litellm must call ChatLiteLLM() exactly once."""
        with patch(_PROVIDER_FACTORY_PATH) as mock_cls:
            make_chat_litellm(_make_settings("openai"))

        mock_cls.assert_called_once()


# ---------------------------------------------------------------------------
# OpenAI provider routing
# ---------------------------------------------------------------------------


class TestOpenAIProviderRouting:
    """Verify kwargs for LLM_PROVIDER=openai."""

    def test_openai_model_kwarg(self) -> None:
        """model kwarg must be 'openai/<model>'."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("openai", OPENAI_TEST_MODEL))

        assert captured["model"] == f"openai/{OPENAI_TEST_MODEL}"

    def test_openai_api_key_kwarg(self) -> None:
        """api_key kwarg must be the OpenAI API key."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("openai", openai_api_key=OPENAI_TEST_KEY))

        assert captured.get("api_key") == OPENAI_TEST_KEY

    def test_openai_no_api_base(self) -> None:
        """OpenAI must NOT receive api_base (uses its default endpoint)."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("openai"))

        assert "api_base" not in captured, (
            f"OpenAI must not set api_base, got {captured.get('api_base')!r}"
        )

    def test_openai_no_api_version(self) -> None:
        """api_version is Azure-only; must not appear for OpenAI."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("openai"))

        assert "api_version" not in captured

    def test_openai_empty_key_excluded(self) -> None:
        """Empty OPENAI_API_KEY → api_key excluded so litellm reads from env."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("openai", openai_api_key=""))

        assert "api_key" not in captured


# ---------------------------------------------------------------------------
# Ollama provider routing
# ---------------------------------------------------------------------------


class TestOllamaProviderRouting:
    """Verify kwargs for LLM_PROVIDER=ollama."""

    def test_ollama_model_kwarg(self) -> None:
        """model kwarg must be 'ollama/<model>'."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("ollama", OLLAMA_TEST_MODEL, openai_api_key=""))

        assert captured["model"] == f"ollama/{OLLAMA_TEST_MODEL}"

    def test_ollama_api_base_kwarg(self) -> None:
        """Ollama must receive api_base pointing to the local server."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(
                _make_settings("ollama", openai_api_key="", ollama_base_url=OLLAMA_BASE_URL)
            )

        assert captured.get("api_base") == OLLAMA_BASE_URL

    def test_ollama_sentinel_api_key(self) -> None:
        """Ollama must receive api_key='ollama' (litellm local-server sentinel)."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("ollama", openai_api_key=""))

        assert captured.get("api_key") == "ollama"


# ---------------------------------------------------------------------------
# Anthropic provider routing
# ---------------------------------------------------------------------------


class TestAnthropicProviderRouting:
    """Verify kwargs for LLM_PROVIDER=anthropic."""

    def test_anthropic_model_kwarg(self) -> None:
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(
                _make_settings(
                    "anthropic",
                    "claude-3-5-sonnet-20241022",
                    openai_api_key="",
                    anthropic_api_key="sk-ant-test",
                )
            )

        assert captured["model"] == "anthropic/claude-3-5-sonnet-20241022"

    def test_anthropic_api_key_kwarg(self) -> None:
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(
                _make_settings(
                    "anthropic",
                    openai_api_key="",
                    anthropic_api_key="sk-ant-real",
                )
            )

        assert captured.get("api_key") == "sk-ant-real"

    def test_anthropic_no_api_base(self) -> None:
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(
                _make_settings("anthropic", openai_api_key="", anthropic_api_key="sk-ant")
            )

        assert "api_base" not in captured


# ---------------------------------------------------------------------------
# Azure provider routing
# ---------------------------------------------------------------------------


class TestAzureProviderRouting:
    """Verify kwargs for LLM_PROVIDER=azure."""

    def test_azure_model_uses_deployment_name(self) -> None:
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(
                _make_settings(
                    "azure",
                    "gpt-4o",
                    openai_api_key="",
                    azure_api_key="az-key",
                    azure_endpoint="https://my.openai.azure.com/",
                    azure_deployment="prod-deploy",
                )
            )

        assert captured["model"] == "azure/prod-deploy"

    def test_azure_api_base_kwarg(self) -> None:
        captured, fn = _capture_kwargs()
        endpoint = "https://my.openai.azure.com/"
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(
                _make_settings(
                    "azure",
                    openai_api_key="",
                    azure_api_key="az-key",
                    azure_endpoint=endpoint,
                    azure_deployment="deploy",
                )
            )

        assert captured.get("api_base") == endpoint

    def test_azure_api_version_kwarg(self) -> None:
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(
                _make_settings(
                    "azure",
                    openai_api_key="",
                    azure_api_key="az-key",
                    azure_endpoint="https://my.azure.com/",
                    azure_deployment="deploy",
                    azure_api_version="2024-08-01-preview",
                )
            )

        assert captured.get("api_version") == "2024-08-01-preview"


# ---------------------------------------------------------------------------
# Override kwargs
# ---------------------------------------------------------------------------


class TestOverrideKwargs:
    """Override kwargs must be merged after settings-derived kwargs."""

    def test_temperature_override(self) -> None:
        """temperature override must appear in ChatLiteLLM kwargs."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("openai"), temperature=0.0)

        assert captured.get("temperature") == 0.0

    def test_max_tokens_override(self) -> None:
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("openai"), max_tokens=256)

        assert captured.get("max_tokens") == 256

    def test_override_takes_precedence_over_settings(self) -> None:
        """An override kwarg must overwrite a settings-derived value."""
        captured, fn = _capture_kwargs()
        settings = _make_settings("openai")
        # Default temperature in settings is 0.7; override to 0.0
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(settings, temperature=0.0)

        # Override wins
        assert captured.get("temperature") == 0.0

    def test_model_always_present_without_override(self) -> None:
        """model kwarg must always be present in ChatLiteLLM kwargs."""
        captured, fn = _capture_kwargs()
        with patch(_PROVIDER_FACTORY_PATH, side_effect=fn):
            make_chat_litellm(_make_settings("openai"))

        assert "model" in captured


# ---------------------------------------------------------------------------
# Settings=None fallback
# ---------------------------------------------------------------------------


class TestSettingsNoneFallback:
    """When settings=None, the global get_settings() singleton must be used."""

    def test_none_settings_reads_from_get_settings(self) -> None:
        """make_chat_litellm(settings=None) calls get_settings().llm."""
        _GET_SETTINGS = "infra.llm.provider_factory.get_settings"
        mock_llm_settings = _make_settings("openai")

        with (
            patch(_GET_SETTINGS) as mock_gs,
            patch(_PROVIDER_FACTORY_PATH),
        ):
            mock_gs.return_value.llm = mock_llm_settings
            make_chat_litellm()  # settings=None

        mock_gs.assert_called_once()

    def test_none_settings_uses_llm_sub_settings(self) -> None:
        """make_chat_litellm() constructs ChatLiteLLM from get_settings().llm kwargs."""
        _GET_SETTINGS = "infra.llm.provider_factory.get_settings"
        mock_llm_settings = _make_settings("openai", "gpt-4o")

        captured, fn = _capture_kwargs()
        with (
            patch(_GET_SETTINGS) as mock_gs,
            patch(_PROVIDER_FACTORY_PATH, side_effect=fn),
        ):
            mock_gs.return_value.llm = mock_llm_settings
            make_chat_litellm()

        assert captured["model"] == "openai/gpt-4o"


# ---------------------------------------------------------------------------
# Module isolation — no domain imports
# ---------------------------------------------------------------------------


class TestInfraLayerIsolation:
    """The infra factory module must not import from chat domain code."""

    def test_provider_factory_module_has_no_domain_imports(self) -> None:
        """provider_factory.py must not import from domains.chat.*

        This verifies the infra layer boundary: infra depends on core config
        (LLMSettings) only, never on domain-layer code.
        """
        import infra.llm.provider_factory as factory_module

        module_vars = vars(factory_module)
        forbidden_domain_symbols = [
            "ProviderFactory",
            "LLMClient",
            "DefaultLLMClientFactory",
            "ChatService",
        ]
        for name in forbidden_domain_symbols:
            assert name not in module_vars, (
                f"provider_factory.py imported domain symbol {name!r} — "
                "the infra layer must only depend on core.config, not on domain code"
            )

    def test_provider_factory_module_exports_make_chat_litellm(self) -> None:
        """make_chat_litellm must be importable from the infra module."""
        from infra.llm.provider_factory import (
            make_chat_litellm as fn,
        )

        assert callable(fn)

    def test_provider_factory_module_exports_chat_litellm_class(self) -> None:
        """ChatLiteLLM must be importable from the infra module for test patching."""
        import infra.llm.provider_factory as factory_module

        assert hasattr(factory_module, "ChatLiteLLM"), (
            "ChatLiteLLM must be present in provider_factory module namespace "
            "so tests can patch it at 'infra.llm.provider_factory.ChatLiteLLM'"
        )
