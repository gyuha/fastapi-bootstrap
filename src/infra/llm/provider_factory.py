"""LangChain-LiteLLM adapter factory — infrastructure layer.

This is the **single source of truth** for creating
:class:`langchain_litellm.ChatLiteLLM` instances.  It reads
``LLM_PROVIDER`` from :class:`~app.core.config.LLMSettings` and
returns a fully configured :class:`ChatLiteLLM` with the correct provider
routing (model identifier, API key, base URL, API version, etc.).

Architecture
------------
This module lives in the *infra* layer (``app.infra.llm``) rather
than inside a bounded context (``domains/<bc>/``) so that:

* The chat domain depends only on its ports/interfaces
  (:class:`~app.domains.chat.ports.LLMClientProtocol`) — never on
  ``langchain_litellm`` directly.
* Switching LLM libraries only requires updating this single module.
* LLM provider-specific kwargs remain co-located with the adapter,
  not scattered across domain files.

Dependency graph::

    [ core.config.LLMSettings ]
              ↓  (settings only — no domain import)
    [ infra.llm.provider_factory ]  ← imports langchain_litellm here only
              ↓  (via DI / container)
    [ domains.chat.llm_client ]     ← LLMClient wraps ChatLiteLLM
              ↓
    [ domains.chat.ports ]          ← LLMClientProtocol (interface)

Provider switching
------------------
Changing ``LLM_PROVIDER`` in ``.env`` is the **only** code change needed
to switch providers.  All routing logic is encapsulated in
:class:`~app.core.config.LLMSettings` (``as_litellm_kwargs``).

Supported providers
-------------------
* ``openai``    — ``ChatLiteLLM(model="openai/gpt-4o-mini", api_key=...)``
* ``anthropic`` — ``ChatLiteLLM(model="anthropic/claude-...", api_key=...)``
* ``gemini``    — ``ChatLiteLLM(model="gemini/...", api_key=...)``
* ``azure``     — ``ChatLiteLLM(model="azure/<deployment>", api_base=..., api_version=...)``
* ``ollama``    — ``ChatLiteLLM(model="ollama/<model>", api_base=<local_url>, api_key="ollama")``

Usage::

    from infra.llm.provider_factory import make_chat_litellm
    from core.config import LLMSettings, LLMProvider

    # Production — reads LLM_PROVIDER from the environment / .env file
    chat = make_chat_litellm()
    result = await chat.ainvoke([HumanMessage(content="Hello")])

    # Explicit settings — useful for testing or per-conversation overrides
    settings = LLMSettings(
        provider=LLMProvider.openai,
        default_model="gpt-4o-mini",
        OPENAI_API_KEY="sk-test",
    )
    chat = make_chat_litellm(settings, temperature=0.0)
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_litellm import ChatLiteLLM

from core.config import LLMSettings, get_settings

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def make_chat_litellm(
    settings: LLMSettings | None = None,
    **override_kwargs: Any,
) -> ChatLiteLLM:
    """Return a :class:`ChatLiteLLM` configured for the active LLM provider.

    Reads ``LLM_PROVIDER`` (and the matching credential / endpoint) from
    :class:`~app.core.config.LLMSettings` and constructs the
    appropriate :class:`ChatLiteLLM`.  Provider switching is transparent —
    change ``LLM_PROVIDER`` in ``.env``; no application code change is needed.

    Parameters
    ----------
    settings:
        Pre-populated :class:`~app.core.config.LLMSettings`
        instance.  When *None*, the global settings singleton is used (reads
        environment variables / ``.env`` file via
        :func:`~app.core.config.get_settings`).
    **override_kwargs:
        Additional keyword arguments forwarded to :class:`ChatLiteLLM`
        *after* the provider-derived kwargs.  Use for per-request tuning,
        for example ``temperature=0.0``, ``max_tokens=256``.
        Override kwargs take precedence over settings-derived values.

    Returns
    -------
    ChatLiteLLM
        A fully configured :class:`langchain_litellm.ChatLiteLLM` instance
        ready for ``ainvoke`` / ``astream`` / ``invoke`` calls.

    Notes
    -----
    This function is the **only** place in the application that calls
    ``ChatLiteLLM()``.  Test suites patch
    ``app.infra.llm.provider_factory.ChatLiteLLM``
    to intercept construction without making real network calls.

    Examples
    --------
    Production usage (reads from environment)::

        from langchain_core.messages import HumanMessage
        from infra.llm.provider_factory import make_chat_litellm

        chat = make_chat_litellm()
        result = await chat.ainvoke([HumanMessage(content="Hello")])
        print(result.content)

    Explicit settings for testing::

        from core.config import LLMSettings, LLMProvider

        settings = LLMSettings(
            provider=LLMProvider.openai,
            default_model="gpt-4o-mini",
            OPENAI_API_KEY="sk-test",
        )
        chat = make_chat_litellm(settings)

    Per-request temperature override::

        chat = make_chat_litellm(temperature=0.0, max_tokens=256)

    Provider-specific behaviour
    ---------------------------
    * **openai / anthropic / gemini** — Sets ``model`` and ``api_key``.
    * **azure** — Sets ``model="azure/<deployment>"``, ``api_key``,
      ``api_base``, and ``api_version``.
    * **ollama** — Sets ``model="ollama/<model>"``, ``api_base=<OLLAMA_BASE_URL>``,
      and ``api_key="ollama"`` (litellm local-server sentinel).
    """
    resolved: LLMSettings = settings or get_settings().llm
    kwargs: dict[str, Any] = resolved.as_litellm_kwargs()
    kwargs.update(override_kwargs)

    logger.debug(
        "chat_litellm_created",
        provider=resolved.provider.value,
        model=kwargs.get("model"),
    )
    return ChatLiteLLM(**kwargs)
