"""Tests for the LiteLLM model string routing factory.

Verifies that :class:`ProviderFactory` correctly maps every supported
(provider, model) combination to the canonical LiteLLM model identifier
string expected by ``ChatLiteLLM(model=...)``.

All tests are pure unit tests — no I/O, no network, no DB.

Covered scenarios
-----------------
* Standard providers: openai, anthropic, gemini, ollama
* Azure special routing (deployment name / fallback to model name)
* Raw string provider values and LLMProvider enum values
* LLMSettings integration via :meth:`ProviderFactory.from_settings`
* Full kwargs dict via :meth:`ProviderFactory.make_kwargs`
* Validation: unknown provider raises ValueError
* :meth:`ProviderFactory.supported_providers` / :meth:`ProviderFactory.is_supported`
"""

from __future__ import annotations

import pytest

from core.config import LLMProvider, LLMSettings
from domains.chat.llm_factory import ProviderFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm(
    provider: str | LLMProvider,
    model: str,
    *,
    openai_api_key: str = "",
    anthropic_api_key: str = "",
    gemini_api_key: str = "",
    azure_api_key: str = "",
    azure_endpoint: str = "",
    azure_deployment: str = "",
    azure_api_version: str = "2024-08-01-preview",
    ollama_base_url: str = "http://localhost:11434",
) -> LLMSettings:
    """Construct an LLMSettings instance without touching the environment."""
    p = LLMProvider(provider) if isinstance(provider, str) else provider
    return LLMSettings(
        provider=p,
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


# ---------------------------------------------------------------------------
# ProviderFactory.make_model_string — raw values
# ---------------------------------------------------------------------------


class TestMakeModelStringStandard:
    """Standard providers follow the '<provider>/<model>' format verbatim."""

    @pytest.mark.parametrize(
        ("provider", "model", "expected"),
        [
            ("openai", "gpt-4o", "openai/gpt-4o"),
            ("openai", "gpt-4o-mini", "openai/gpt-4o-mini"),
            ("openai", "gpt-3.5-turbo", "openai/gpt-3.5-turbo"),
            ("anthropic", "claude-3-5-sonnet-20241022", "anthropic/claude-3-5-sonnet-20241022"),
            ("anthropic", "claude-3-5-haiku-20241022", "anthropic/claude-3-5-haiku-20241022"),
            ("anthropic", "claude-3-opus-20240229", "anthropic/claude-3-opus-20240229"),
            ("gemini", "gemini-1.5-flash", "gemini/gemini-1.5-flash"),
            ("gemini", "gemini-1.5-pro", "gemini/gemini-1.5-pro"),
            ("gemini", "gemini-2.0-flash", "gemini/gemini-2.0-flash"),
            ("ollama", "llama3.2", "ollama/llama3.2"),
            ("ollama", "mistral", "ollama/mistral"),
            ("ollama", "codellama:13b", "ollama/codellama:13b"),
        ],
    )
    def test_standard_routing(self, provider: str, model: str, expected: str) -> None:
        assert ProviderFactory.make_model_string(provider, model) == expected

    def test_accepts_enum_provider(self) -> None:
        result = ProviderFactory.make_model_string(LLMProvider.openai, "gpt-4o")
        assert result == "openai/gpt-4o"

    def test_accepts_enum_anthropic(self) -> None:
        result = ProviderFactory.make_model_string(
            LLMProvider.anthropic, "claude-3-5-sonnet-20241022"
        )
        assert result == "anthropic/claude-3-5-sonnet-20241022"

    def test_accepts_enum_gemini(self) -> None:
        result = ProviderFactory.make_model_string(LLMProvider.gemini, "gemini-1.5-flash")
        assert result == "gemini/gemini-1.5-flash"

    def test_accepts_enum_ollama(self) -> None:
        result = ProviderFactory.make_model_string(LLMProvider.ollama, "llama3.2")
        assert result == "ollama/llama3.2"


class TestMakeModelStringAzure:
    """Azure routing uses deployment name, falling back to model when not set."""

    def test_azure_with_deployment_name(self) -> None:
        result = ProviderFactory.make_model_string(
            "azure", "gpt-4o", azure_deployment="my-prod-deployment"
        )
        assert result == "azure/my-prod-deployment"

    def test_azure_deployment_takes_precedence_over_model(self) -> None:
        result = ProviderFactory.make_model_string(
            "azure", "gpt-4o-mini", azure_deployment="custom-deploy"
        )
        # deployment name wins
        assert result == "azure/custom-deploy"
        assert "gpt-4o-mini" not in result

    def test_azure_no_deployment_falls_back_to_model(self) -> None:
        result = ProviderFactory.make_model_string("azure", "gpt-4o")
        assert result == "azure/gpt-4o"

    def test_azure_empty_string_deployment_falls_back_to_model(self) -> None:
        result = ProviderFactory.make_model_string("azure", "gpt-4o-mini", azure_deployment="")
        assert result == "azure/gpt-4o-mini"

    def test_azure_whitespace_only_deployment_falls_back_to_model(self) -> None:
        result = ProviderFactory.make_model_string("azure", "gpt-4o", azure_deployment="   ")
        assert result == "azure/gpt-4o"

    def test_azure_accepts_enum(self) -> None:
        result = ProviderFactory.make_model_string(
            LLMProvider.azure, "gpt-4o", azure_deployment="enterprise-deploy"
        )
        assert result == "azure/enterprise-deploy"


class TestMakeModelStringValidation:
    """Unknown provider values must raise ValueError immediately."""

    @pytest.mark.parametrize("bad_provider", ["cohere", "mistralai", "bedrock", "", "OPENAI"])
    def test_unknown_provider_raises(self, bad_provider: str) -> None:
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            ProviderFactory.make_model_string(bad_provider, "some-model")

    def test_error_message_lists_known_providers(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            ProviderFactory.make_model_string("unknown-provider", "model")
        error_msg = str(exc_info.value)
        for p in LLMProvider:
            assert p.value in error_msg


# ---------------------------------------------------------------------------
# ProviderFactory.from_settings — LLMSettings integration
# ---------------------------------------------------------------------------


class TestFromSettings:
    """from_settings() must produce identical output to make_model_string()."""

    def test_openai_from_settings(self) -> None:
        settings = _make_llm("openai", "gpt-4o", openai_api_key="sk-test")
        assert ProviderFactory.from_settings(settings) == "openai/gpt-4o"

    def test_anthropic_from_settings(self) -> None:
        settings = _make_llm(
            "anthropic",
            "claude-3-5-sonnet-20241022",
            anthropic_api_key="sk-ant-test",
        )
        assert ProviderFactory.from_settings(settings) == "anthropic/claude-3-5-sonnet-20241022"

    def test_gemini_from_settings(self) -> None:
        settings = _make_llm("gemini", "gemini-1.5-flash", gemini_api_key="AIza-test")
        assert ProviderFactory.from_settings(settings) == "gemini/gemini-1.5-flash"

    def test_ollama_from_settings(self) -> None:
        settings = _make_llm(
            "ollama",
            "llama3.2",
            ollama_base_url="http://localhost:11434",
        )
        assert ProviderFactory.from_settings(settings) == "ollama/llama3.2"

    def test_azure_with_deployment_from_settings(self) -> None:
        settings = _make_llm(
            "azure",
            "gpt-4o",
            azure_api_key="az-key",
            azure_endpoint="https://my.openai.azure.com/",
            azure_deployment="my-deployment",
        )
        assert ProviderFactory.from_settings(settings) == "azure/my-deployment"

    def test_azure_no_deployment_from_settings(self) -> None:
        settings = _make_llm(
            "azure",
            "gpt-4o-mini",
            azure_api_key="az-key",
            azure_deployment="",
        )
        # Falls back to model name
        assert ProviderFactory.from_settings(settings) == "azure/gpt-4o-mini"

    def test_from_settings_matches_make_model_string(self) -> None:
        """from_settings and make_model_string must return the same value."""
        for provider_val in ["openai", "anthropic", "gemini", "ollama"]:
            model = "test-model"
            settings = _make_llm(provider_val, model)
            via_factory = ProviderFactory.make_model_string(provider_val, model)
            via_settings = ProviderFactory.from_settings(settings)
            assert via_factory == via_settings, (
                f"Mismatch for provider={provider_val!r}: "
                f"make_model_string={via_factory!r}, from_settings={via_settings!r}"
            )


# ---------------------------------------------------------------------------
# ProviderFactory.make_kwargs — full ChatLiteLLM kwargs dict
# ---------------------------------------------------------------------------


class TestMakeKwargs:
    """make_kwargs() must produce dicts usable by ChatLiteLLM(**kwargs)."""

    def test_openai_kwargs_contains_model_and_api_key(self) -> None:
        settings = _make_llm("openai", "gpt-4o", openai_api_key="sk-real-key")
        kwargs = ProviderFactory.make_kwargs(settings)
        assert kwargs["model"] == "openai/gpt-4o"
        assert kwargs["api_key"] == "sk-real-key"

    def test_anthropic_kwargs(self) -> None:
        settings = _make_llm(
            "anthropic", "claude-3-5-haiku-20241022", anthropic_api_key="sk-ant-real"
        )
        kwargs = ProviderFactory.make_kwargs(settings)
        assert kwargs["model"] == "anthropic/claude-3-5-haiku-20241022"
        assert kwargs["api_key"] == "sk-ant-real"

    def test_gemini_kwargs(self) -> None:
        settings = _make_llm("gemini", "gemini-1.5-pro", gemini_api_key="AIza-real")
        kwargs = ProviderFactory.make_kwargs(settings)
        assert kwargs["model"] == "gemini/gemini-1.5-pro"
        assert kwargs["api_key"] == "AIza-real"

    def test_azure_kwargs_includes_api_base_and_version(self) -> None:
        settings = _make_llm(
            "azure",
            "gpt-4o",
            azure_api_key="az-real-key",
            azure_endpoint="https://my-resource.openai.azure.com/",
            azure_deployment="prod-gpt4o",
            azure_api_version="2024-08-01-preview",
        )
        kwargs = ProviderFactory.make_kwargs(settings)
        assert kwargs["model"] == "azure/prod-gpt4o"
        assert kwargs["api_key"] == "az-real-key"
        assert kwargs["api_base"] == "https://my-resource.openai.azure.com/"
        assert kwargs["api_version"] == "2024-08-01-preview"

    def test_ollama_kwargs_includes_api_base_no_real_key(self) -> None:
        settings = _make_llm(
            "ollama",
            "mistral",
            ollama_base_url="http://custom-ollama:11434",
        )
        kwargs = ProviderFactory.make_kwargs(settings)
        assert kwargs["model"] == "ollama/mistral"
        assert kwargs["api_base"] == "http://custom-ollama:11434"
        # litellm expects the sentinel "ollama" string, not a real key
        assert kwargs["api_key"] == "ollama"

    def test_openai_no_api_key_excludes_api_key_from_kwargs(self) -> None:
        settings = _make_llm("openai", "gpt-4o-mini", openai_api_key="")
        kwargs = ProviderFactory.make_kwargs(settings)
        assert kwargs["model"] == "openai/gpt-4o-mini"
        # Empty key → not included (litellm reads from env instead)
        assert "api_key" not in kwargs

    def test_kwargs_model_always_present(self) -> None:
        for provider_val in ["openai", "anthropic", "gemini", "ollama"]:
            settings = _make_llm(provider_val, "some-model")
            kwargs = ProviderFactory.make_kwargs(settings)
            assert "model" in kwargs, f"'model' missing for provider={provider_val!r}"


# ---------------------------------------------------------------------------
# ProviderFactory.supported_providers / is_supported
# ---------------------------------------------------------------------------


class TestSupportedProviders:
    def test_returns_sorted_list(self) -> None:
        providers = ProviderFactory.supported_providers()
        assert providers == sorted(providers)

    def test_contains_all_llm_provider_values(self) -> None:
        providers = set(ProviderFactory.supported_providers())
        expected = {p.value for p in LLMProvider}
        assert providers == expected

    def test_is_supported_true_for_known(self) -> None:
        for p in LLMProvider:
            assert ProviderFactory.is_supported(p.value), f"Expected {p.value!r} to be supported"

    def test_is_supported_false_for_unknown(self) -> None:
        assert ProviderFactory.is_supported("cohere") is False
        assert ProviderFactory.is_supported("bedrock") is False
        assert ProviderFactory.is_supported("") is False
        assert ProviderFactory.is_supported("OPENAI") is False  # case-sensitive


# ---------------------------------------------------------------------------
# Provider switching — portability guarantee
# ---------------------------------------------------------------------------


class TestProviderSwitching:
    """Demonstrate that switching providers requires only a different provider value.

    This test class documents the llm_provider_portability evaluation principle:
    LLM provider switching happens at the env/settings level, with zero code changes.
    """

    @pytest.mark.parametrize(
        ("provider", "model", "expected_prefix"),
        [
            ("openai", "gpt-4o", "openai/"),
            ("anthropic", "claude-3-5-sonnet-20241022", "anthropic/"),
            ("gemini", "gemini-1.5-pro", "gemini/"),
            ("ollama", "llama3.2", "ollama/"),
            ("azure", "gpt-4o", "azure/"),
        ],
    )
    def test_provider_prefix_in_model_string(
        self, provider: str, model: str, expected_prefix: str
    ) -> None:
        result = ProviderFactory.make_model_string(provider, model)
        assert result.startswith(expected_prefix), (
            f"Provider '{provider}' should produce string starting with '{expected_prefix}', "
            f"got {result!r}"
        )

    def test_model_name_preserved_for_standard_providers(self) -> None:
        """The model name must be preserved verbatim for non-Azure providers."""
        for provider_val in ["openai", "anthropic", "gemini", "ollama"]:
            model = "my-specific-model-v2"
            result = ProviderFactory.make_model_string(provider_val, model)
            assert result.endswith(f"/{model}"), (
                f"Model name not preserved for {provider_val!r}: {result!r}"
            )

    def test_provider_in_model_string_is_always_lowercase(self) -> None:
        """LiteLLM is case-sensitive; provider prefix must always be lowercase."""
        for p in LLMProvider:
            result = ProviderFactory.make_model_string(p, "test-model")
            provider_prefix = result.split("/")[0]
            assert provider_prefix == provider_prefix.lower(), (
                f"Provider prefix {provider_prefix!r} is not lowercase"
            )
