"""LiteLLM model string routing factory for the chat domain.

This module is the **single source of truth** for converting an LLM provider
name + model name into the canonical LiteLLM model identifier string that is
passed directly to ``ChatLiteLLM(model=...)``.

Format: ``<provider>/<model>``  — e.g. ``openai/gpt-4o``, ``anthropic/claude-3-5-sonnet-20241022``

Switching providers requires only changing ``LLM_PROVIDER`` (and the
corresponding API key) in ``.env``.  No application code changes needed.

Usage::

    from domains.chat.llm_factory import ProviderFactory
    from core.config import LLMSettings, LLMProvider

    # Build model string from raw values
    model_str = ProviderFactory.make_model_string("openai", "gpt-4o")
    # → "openai/gpt-4o"

    model_str = ProviderFactory.make_model_string("anthropic", "claude-3-5-sonnet-20241022")
    # → "anthropic/claude-3-5-sonnet-20241022"

    model_str = ProviderFactory.make_model_string(
        "azure", "gpt-4o", azure_deployment="my-deployment"
    )
    # → "azure/my-deployment"

    # Build from a populated LLMSettings instance
    settings = LLMSettings(provider=LLMProvider.openai, default_model="gpt-4o-mini")
    model_str = ProviderFactory.from_settings(settings)
    # → "openai/gpt-4o-mini"

    # Full ChatLiteLLM kwargs dict
    kwargs = ProviderFactory.make_kwargs(settings)
    # → {"model": "openai/gpt-4o-mini", "api_key": "sk-..."}
"""

from __future__ import annotations

from typing import Any

from core.config import LLMProvider, LLMSettings

# ---------------------------------------------------------------------------
# Supported provider registry
# ---------------------------------------------------------------------------

#: Maps LLMProvider value → litellm provider prefix.
#:
#: Azure and Ollama are handled with special logic; remaining providers use
#: the standard ``<provider>/<model>`` format directly.
_PROVIDER_PREFIXES: dict[str, str] = {
    LLMProvider.openai.value: "openai",
    LLMProvider.anthropic.value: "anthropic",
    LLMProvider.gemini.value: "gemini",
    LLMProvider.ollama.value: "ollama",
    # azure is intentionally absent — handled by dedicated branch
}

#: Set of providers that follow the standard ``<provider>/<model>`` routing
#: without any special-case logic.
_STANDARD_PROVIDERS: frozenset[str] = frozenset(_PROVIDER_PREFIXES)


# ---------------------------------------------------------------------------
# ProviderFactory
# ---------------------------------------------------------------------------


class ProviderFactory:
    """Convert provider + model settings into LiteLLM-compatible identifiers.

    All public methods are **class methods** (no instantiation needed).
    The factory holds the authoritative routing rules for every supported
    LLM provider, making provider-specific logic easy to test and extend.

    Supported providers
    -------------------
    * ``openai``    — standard routing: ``openai/<model>``
    * ``anthropic`` — standard routing: ``anthropic/<model>``
    * ``gemini``    — standard routing: ``gemini/<model>``
    * ``azure``     — uses *deployment name* instead of model:
                      ``azure/<deployment>`` (falls back to model if no deployment)
    * ``ollama``    — standard routing: ``ollama/<model>``
                      (no API key; uses ``api_base`` instead)

    Adding a new provider
    ---------------------
    If the new provider follows the standard ``<provider>/<model>`` format,
    add its value to :data:`_PROVIDER_PREFIXES` above.  If it requires
    special routing logic, add a new ``elif`` branch inside
    :meth:`make_model_string`.
    """

    @classmethod
    def make_model_string(
        cls,
        provider: str | LLMProvider,
        model: str,
        *,
        azure_deployment: str = "",
    ) -> str:
        """Return the LiteLLM model identifier string for the given provider/model.

        Parameters
        ----------
        provider:
            LLM provider — accepts either the :class:`LLMProvider` enum member
            or its raw string value (e.g. ``"openai"``).
        model:
            Base model name (e.g. ``"gpt-4o-mini"``, ``"claude-3-5-sonnet-20241022"``).
            For Azure, this is used as a fallback when ``azure_deployment`` is empty.
        azure_deployment:
            Azure OpenAI deployment name.  When provided, takes precedence over
            ``model`` for the Azure routing path.

        Returns
        -------
        str
            LiteLLM model identifier, e.g. ``"openai/gpt-4o"``.

        Raises
        ------
        ValueError
            If *provider* is not a recognised :class:`LLMProvider` value.

        Examples
        --------
        >>> ProviderFactory.make_model_string("openai", "gpt-4o")
        'openai/gpt-4o'
        >>> ProviderFactory.make_model_string("anthropic", "claude-3-5-sonnet-20241022")
        'anthropic/claude-3-5-sonnet-20241022'
        >>> ProviderFactory.make_model_string("azure", "gpt-4o", azure_deployment="prod-deploy")
        'azure/prod-deploy'
        >>> ProviderFactory.make_model_string("azure", "gpt-4o-mini")
        'azure/gpt-4o-mini'
        >>> ProviderFactory.make_model_string("gemini", "gemini-1.5-flash")
        'gemini/gemini-1.5-flash'
        >>> ProviderFactory.make_model_string("ollama", "llama3.2")
        'ollama/llama3.2'
        """
        # Normalise to string value for uniform comparison
        provider_str: str = provider.value if isinstance(provider, LLMProvider) else provider

        # Validate against known providers
        try:
            LLMProvider(provider_str)
        except ValueError:
            known = sorted(p.value for p in LLMProvider)
            raise ValueError(
                f"Unsupported LLM provider: {provider_str!r}. Known providers: {known}"
            ) from None

        # ── Azure: deployment-name routing ────────────────────────────────
        if provider_str == LLMProvider.azure.value:
            deployment = azure_deployment.strip() or model
            return f"azure/{deployment}"

        # ── Standard providers: <provider>/<model> ────────────────────────
        return f"{provider_str}/{model}"

    @classmethod
    def from_settings(cls, settings: LLMSettings) -> str:
        """Return the LiteLLM model string derived from an :class:`LLMSettings` instance.

        Delegates to :meth:`make_model_string` with values extracted from
        *settings*, keeping all routing logic centralised in this factory.

        Parameters
        ----------
        settings:
            Populated :class:`~app.core.config.LLMSettings` instance.

        Returns
        -------
        str
            LiteLLM model identifier.

        Examples
        --------
        >>> from core.config import LLMSettings, LLMProvider
        >>> s = LLMSettings(provider=LLMProvider.openai, default_model="gpt-4o-mini")
        >>> ProviderFactory.from_settings(s)
        'openai/gpt-4o-mini'
        """
        return cls.make_model_string(
            provider=settings.provider,
            model=settings.default_model,
            azure_deployment=settings.azure_openai_deployment,
        )

    @classmethod
    def make_kwargs(cls, settings: LLMSettings) -> dict[str, Any]:
        """Return a kwargs dict ready for ``ChatLiteLLM(**kwargs)``.

        The returned dict always contains ``"model"``.  Provider-specific
        keys (``"api_key"``, ``"api_base"``, ``"api_version"``) are added
        only when relevant.

        Parameters
        ----------
        settings:
            Populated :class:`~app.core.config.LLMSettings` instance.

        Returns
        -------
        dict[str, Any]
            Keyword arguments for ``ChatLiteLLM``.

        Examples
        --------
        >>> from core.config import LLMSettings, LLMProvider
        >>> s = LLMSettings(
        ...     provider=LLMProvider.openai,
        ...     default_model="gpt-4o",
        ...     OPENAI_API_KEY="sk-test",
        ... )
        >>> ProviderFactory.make_kwargs(s)
        {'model': 'openai/gpt-4o', 'api_key': 'sk-test'}
        """
        return settings.as_litellm_kwargs()

    @classmethod
    def supported_providers(cls) -> list[str]:
        """Return a sorted list of all supported LLM provider values.

        Useful for validation, documentation, and dynamic UI generation.

        Returns
        -------
        list[str]
            Sorted list of provider string values.

        Examples
        --------
        >>> ProviderFactory.supported_providers()
        ['anthropic', 'azure', 'gemini', 'ollama', 'openai']
        """
        return sorted(p.value for p in LLMProvider)

    @classmethod
    def is_supported(cls, provider: str) -> bool:
        """Return ``True`` if *provider* is a recognised LLM provider.

        Parameters
        ----------
        provider:
            Provider string value to check.

        Returns
        -------
        bool

        Examples
        --------
        >>> ProviderFactory.is_supported("openai")
        True
        >>> ProviderFactory.is_supported("cohere")
        False
        """
        return provider in cls.supported_providers()
