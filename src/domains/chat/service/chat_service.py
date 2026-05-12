"""Chat domain service.

Encapsulates LLM-backed chat business logic.  The service is the **only**
consumer of :class:`~app.domains.chat.ports.AbstractLLMPort`
inside the chat domain — routers and other callers go through this class.

Domain isolation guarantee
--------------------------
This module imports **only** from:

* :mod:`app.domains.chat.ports` — the abstract port defined by the
  chat domain itself.

LangChain and litellm types are imported **exclusively** inside the
``TYPE_CHECKING`` block so they are never loaded at runtime.  This enforces
the hexagonal architecture boundary: *the domain depends on the port
abstraction, not on any concrete provider library*.

It does **not** import :class:`~app.domains.chat.llm_client.LLMClient`
or any other concrete infrastructure class.

Usage::

    from langchain_core.messages import HumanMessage, SystemMessage

    from domains.chat.llm_client import DefaultLLMClientFactory
    from domains.chat.service import ChatService

    factory = DefaultLLMClientFactory()
    service = ChatService(llm_client=factory.get_llm_client())

    # Non-streaming
    response = await service.complete(
        messages=[
            SystemMessage(content="You are a helpful assistant."),
            HumanMessage(content="What is 2 + 2?"),
        ]
    )
    print(response.content)  # "4"

    # Streaming (use with sse_starlette.sse.EventSourceResponse)
    async for chunk in service.stream(messages):
        print(chunk, end="", flush=True)

FastAPI dependency injection pattern::

    from fastapi import APIRouter, Depends
    from sse_starlette.sse import EventSourceResponse

    from domains.chat.container import get_chat_service
    from domains.chat.service import ChatService

    router = APIRouter()

    @router.post("/chat/complete")
    async def chat_complete(
        body: ChatRequest,
        service: ChatService = Depends(get_chat_service),
    ) -> dict:
        response = await service.complete(messages=body.to_langchain_messages())
        return {"content": response.content}

    @router.post("/chat/stream")
    async def chat_stream(
        body: ChatRequest,
        service: ChatService = Depends(get_chat_service),
    ) -> EventSourceResponse:
        async def _gen():
            async for chunk in service.stream(messages=body.to_langchain_messages()):
                yield {"data": chunk}

        return EventSourceResponse(_gen())
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from domains.chat.ports import AbstractLLMPort

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from langchain_core.messages.ai import AIMessage

logger = structlog.get_logger(__name__)


class ChatService:
    """Domain service for LLM-backed chat operations.

    Orchestrates calls to the LLM client and applies any domain-level logic
    (logging, error translation, token counting, etc.).

    This class depends **only** on :class:`~app.domains.chat.ports.AbstractLLMPort`
    — it never references concrete LLM library classes directly.  This design
    means:

    * Swapping the LLM provider requires zero changes to this class.
    * Tests can inject a lightweight mock without patching import paths.
    * ``mypy --strict`` validates the structural compatibility at check time.
    * The domain layer has **zero runtime imports** from any LLM provider library.

    Parameters
    ----------
    llm_client:
        Any object satisfying :class:`~app.domains.chat.ports.AbstractLLMPort`.
        In production this is an :class:`~app.domains.chat.llm_client.LLMClient`
        instance; in tests it can be any mock with matching method signatures.

    Examples
    --------
    Construct via the default factory::

        from domains.chat.llm_client import DefaultLLMClientFactory
        service = ChatService(llm_client=DefaultLLMClientFactory().get_llm_client())

    Construct with a mock for testing::

        from unittest.mock import AsyncMock
        from domains.chat.ports import AbstractLLMPort

        mock_llm = AsyncMock(spec=AbstractLLMPort)
        mock_llm.invoke.return_value = AIMessage(content="test response")
        service = ChatService(llm_client=mock_llm)
    """

    def __init__(self, llm_client: AbstractLLMPort) -> None:
        self._llm: AbstractLLMPort = llm_client

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AIMessage:
        """Send a conversation to the LLM and return the full response.

        Delegates to :meth:`AbstractLLMPort.invoke` and logs the call
        at debug level.  Any provider-level exception propagates to the caller
        unchanged — error translation is the router's responsibility.

        Parameters
        ----------
        messages:
            Ordered conversation history as LangChain
            :class:`~langchain_core.messages.BaseMessage` instances.  Typically
            starts with a :class:`~langchain_core.messages.SystemMessage` followed
            by alternating :class:`~langchain_core.messages.HumanMessage` /
            :class:`~langchain_core.messages.AIMessage` pairs.
        **kwargs:
            Extra options forwarded verbatim to the LLM client (e.g.
            ``temperature``, ``max_tokens``, ``stop``).

        Returns
        -------
        AIMessage
            The model's complete response.

        Raises
        ------
        Exception
            Any exception raised by the underlying LLM provider (network
            errors, rate limits, context-length exceeded, etc.).

        Examples
        --------
        >>> from langchain_core.messages import HumanMessage, SystemMessage
        >>> response = await service.complete([
        ...     SystemMessage(content="Be concise."),
        ...     HumanMessage(content="Capital of France?"),
        ... ])
        >>> print(response.content)
        'Paris'
        """
        logger.debug(
            "chat_service_complete",
            message_count=len(messages),
        )
        result: AIMessage = await self._llm.invoke(messages, **kwargs)
        logger.debug(
            "chat_service_complete_done",
            content_length=len(str(result.content)),
        )
        return result

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> Any:  # AsyncGenerator[str, None] — declared as Any to avoid complex generics
        """Stream the LLM response, yielding non-empty text chunks.

        Wraps :meth:`AbstractLLMPort.stream` with domain-level debug
        logging.  Designed for use with :class:`sse_starlette.sse.EventSourceResponse`
        to deliver Server-Sent Events to the client.

        Parameters
        ----------
        messages:
            Ordered conversation history as LangChain
            :class:`~langchain_core.messages.BaseMessage` instances.
        **kwargs:
            Extra options forwarded verbatim to the LLM client.

        Yields
        ------
        str
            Non-empty text fragments as they arrive from the LLM provider.

        Examples
        --------
        >>> async def sse_generator():
        ...     async for chunk in service.stream(messages):
        ...         yield {"data": chunk}
        >>> return EventSourceResponse(sse_generator())
        """
        logger.debug(
            "chat_service_stream",
            message_count=len(messages),
        )
        chunk_count = 0
        async for chunk in self._llm.stream(messages, **kwargs):
            chunk_count += 1
            yield chunk
        logger.debug(
            "chat_service_stream_done",
            chunks=chunk_count,
        )
