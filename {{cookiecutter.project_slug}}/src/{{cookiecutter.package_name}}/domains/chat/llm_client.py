"""LangChain LiteLLM client abstraction for the chat domain.

Provides :class:`LLMClient` — a thin, provider-agnostic wrapper around
:class:`langchain_litellm.ChatLiteLLM` that:

1. Sources its configuration from :class:`~{{ cookiecutter.package_name }}.core.config.LLMSettings`
   via :class:`~{{ cookiecutter.package_name }}.domains.chat.llm_factory.ProviderFactory`.
2. Exposes a clean async interface (``ainvoke`` / ``astream``).
3. Registers a FastAPI dependency so routers receive a pre-configured client
   via ``Depends(get_llm_client)``.

Provider switching is transparent — change ``LLM_PROVIDER`` + the matching
API key in ``.env``.  No application-code changes are required.

Usage::

    # In a FastAPI route
    from fastapi import Depends
    from langchain_core.messages import HumanMessage, SystemMessage

    from {{ cookiecutter.package_name }}.domains.chat.llm_client import LLMClient, get_llm_client

    @router.post("/chat")
    async def chat_endpoint(
        body: ChatRequest,
        llm: LLMClient = Depends(get_llm_client),
    ) -> dict:
        messages = [
            SystemMessage(content="You are a helpful assistant."),
            HumanMessage(content=body.user_message),
        ]
        # Non-streaming:
        response = await llm.ainvoke(messages)
        return {"reply": response.content}

        # Streaming (SSE):
        # from sse_starlette.sse import EventSourceResponse
        # async def _gen():
        #     async for chunk in llm.astream(messages):
        #         yield {"data": chunk}
        # return EventSourceResponse(_gen())
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from langchain_core.messages import BaseMessage
from langchain_core.messages.ai import AIMessage
from langchain_litellm import ChatLiteLLM

from {{ cookiecutter.package_name }}.core.config import LLMSettings, get_settings
from {{ cookiecutter.package_name }}.domains.chat.llm_factory import ProviderFactory

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------


class LLMClient:
    """Provider-agnostic async LLM client backed by langchain-litellm.

    This class is a thin, testable wrapper around :class:`ChatLiteLLM` that:

    * Delegates model-string construction to :class:`ProviderFactory` so that
      all routing rules live in a single place.
    * Exposes only the async interface (``ainvoke`` / ``astream``) that the
      chat domain actually needs.
    * Accepts ``override_kwargs`` for per-conversation tuning (e.g. temperature,
      max_tokens) without modifying global settings.

    Parameters
    ----------
    settings:
        :class:`LLMSettings` instance.  If *None*, reads from environment
        variables via a fresh :class:`LLMSettings()` constructor call.
    **override_kwargs:
        Additional keyword arguments forwarded to :class:`ChatLiteLLM`.
        These *override* anything derived from *settings* (e.g.
        ``temperature=0.2``, ``max_tokens=512``).

    Examples
    --------
    >>> # Uses global env-var settings
    >>> client = LLMClient()

    >>> # Custom settings (e.g. per-test)
    >>> from {{ cookiecutter.package_name }}.core.config import LLMSettings, LLMProvider
    >>> s = LLMSettings(provider=LLMProvider.openai, default_model="gpt-4o-mini")
    >>> client = LLMClient(settings=s, temperature=0.1)
    """

    def __init__(
        self,
        settings: LLMSettings | None = None,
        **override_kwargs: Any,
    ) -> None:
        resolved: LLMSettings = settings or LLMSettings()
        base_kwargs: dict[str, Any] = ProviderFactory.make_kwargs(resolved)
        base_kwargs.update(override_kwargs)

        self._model_string: str = str(base_kwargs["model"])
        self._provider: str = resolved.provider.value
        self._chat: ChatLiteLLM = ChatLiteLLM(**base_kwargs)

        logger.debug(
            "llm_client_initialized",
            model=self._model_string,
            provider=self._provider,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def model_string(self) -> str:
        """Full LiteLLM model identifier (e.g. ``'openai/gpt-4o-mini'``)."""
        return self._model_string

    @property
    def provider(self) -> str:
        """Active provider name (e.g. ``'openai'``, ``'anthropic'``)."""
        return self._provider

    @property
    def chat(self) -> ChatLiteLLM:
        """Underlying :class:`ChatLiteLLM` instance for advanced use cases."""
        return self._chat

    # ------------------------------------------------------------------
    # Async interface
    # ------------------------------------------------------------------

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AIMessage:
        """Invoke the LLM and return the full response as an :class:`AIMessage`.

        Parameters
        ----------
        messages:
            Ordered list of LangChain :class:`BaseMessage` instances
            (e.g. :class:`SystemMessage`, :class:`HumanMessage`,
            :class:`AIMessage` for multi-turn context).
        **kwargs:
            Additional kwargs forwarded to :meth:`ChatLiteLLM.ainvoke`.

        Returns
        -------
        AIMessage
            The model's response.

        Example
        -------
        >>> from langchain_core.messages import HumanMessage
        >>> response = await client.ainvoke([HumanMessage(content="Hello!")])
        >>> print(response.content)
        """
        logger.debug(
            "llm_ainvoke_start",
            model=self._model_string,
            message_count=len(messages),
        )
        result = await self._chat.ainvoke(messages, **kwargs)
        logger.debug(
            "llm_ainvoke_complete",
            model=self._model_string,
            content_length=len(str(result.content)),
        )
        return result  # type: ignore[return-value]

    async def astream(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream the LLM response, yielding non-empty text chunks.

        Designed for use with :class:`sse_starlette.sse.EventSourceResponse`
        to deliver Server-Sent Events to the client.

        Parameters
        ----------
        messages:
            Ordered list of LangChain :class:`BaseMessage` instances.
        **kwargs:
            Additional kwargs forwarded to :meth:`ChatLiteLLM.astream`.

        Yields
        ------
        str
            Individual text chunks as they arrive from the provider.

        Example
        -------
        >>> from sse_starlette.sse import EventSourceResponse
        >>>
        >>> async def _gen():
        ...     async for chunk in client.astream(messages):
        ...         yield {"data": chunk}
        >>>
        >>> return EventSourceResponse(_gen())
        """
        logger.debug(
            "llm_astream_start",
            model=self._model_string,
            message_count=len(messages),
        )
        chunk_count = 0
        async for chunk in self._chat.astream(messages, **kwargs):
            content = chunk.content
            if content:
                chunk_count += 1
                yield str(content)
        logger.debug(
            "llm_astream_complete",
            model=self._model_string,
            chunks=chunk_count,
        )


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def get_llm_client() -> LLMClient:
    """FastAPI dependency that returns a configured :class:`LLMClient`.

    Reads LLM settings from the global :class:`Settings` singleton (populated
    from environment variables / ``.env`` file).  The client is constructed
    fresh per-request so that dynamic env changes in tests are picked up.

    Usage::

        from fastapi import APIRouter, Depends
        from {{ cookiecutter.package_name }}.domains.chat.llm_client import LLMClient, get_llm_client

        router = APIRouter()

        @router.post("/messages")
        async def create_message(
            llm: LLMClient = Depends(get_llm_client),
        ) -> dict:
            ...

    Notes
    -----
    In tests, override this dependency via ``app.dependency_overrides``::

        from unittest.mock import AsyncMock, MagicMock
        from {{ cookiecutter.package_name }}.domains.chat.llm_client import LLMClient, get_llm_client

        mock_llm = MagicMock(spec=LLMClient)
        mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="mocked"))
        app.dependency_overrides[get_llm_client] = lambda: mock_llm
    """
    app_settings = get_settings()
    return LLMClient(settings=app_settings.llm)
