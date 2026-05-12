"""Abstract ports (interfaces) for the chat domain.

Defines the structural contracts that the chat domain relies on, following
hexagonal architecture (Ports & Adapters).  The domain declares *what* it
needs; infrastructure provides the concrete adapters.

Three public interfaces are defined here:

* :class:`LLMClientProtocol` — the minimal ``@runtime_checkable`` Protocol
  any LLM client must satisfy: async ``ainvoke`` and ``astream``.  Enables
  structural (duck-typing) subtyping without explicit inheritance.  The
  concrete implementation is
  :class:`~app.domains.chat.llm_client.LLMClient`.

* :class:`LLMClientFactoryProtocol` — a factory Protocol that exposes a
  single ``get_llm_client()`` method returning an :class:`LLMClientProtocol`.
  The concrete implementation is
  :class:`~app.domains.chat.llm_client.DefaultLLMClientFactory`.

* :class:`AbstractLLMPort` — an explicit ABC (Abstract Base Class) that
  enforces the LLM port contract via ``@abstractmethod``.  Defines ``invoke``
  (non-streaming) and ``stream`` (async-iterator) as the canonical domain
  vocabulary.  Preferred for first-party adapters that inherit explicitly.

The chat domain service (:class:`~app.domains.chat.service.ChatService`)
depends *only* on these protocol types, never on concrete implementations.
This enables:

* **Testability** — replace with a mock that satisfies the protocol.
* **Provider portability** — swap the underlying LLM library without
  touching domain logic.
* **Static type safety** — ``mypy`` validates structural compatibility
  at check time via ``typing.Protocol``.

Usage::

    from domains.chat.ports import (
        AbstractLLMPort,
        LLMClientProtocol,
        LLMClientFactoryProtocol,
    )

    # Type-safe dependency injection in the service
    def build_chat_service(factory: LLMClientFactoryProtocol) -> ChatService:
        return ChatService(llm_client=factory.get_llm_client())

    # Inline structural check (useful in tests)
    from domains.chat.llm_client import LLMClient
    assert isinstance(LLMClient(...), LLMClientProtocol)

    # Explicit ABC subclass for a first-party adapter
    class MyAdapter(AbstractLLMPort):
        async def invoke(self, messages, **kwargs): ...
        async def stream(self, messages, **kwargs): yield "chunk"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from langchain_core.messages.ai import AIMessage


# ---------------------------------------------------------------------------
# LLMClientProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Structural interface for async LLM clients used by the chat domain.

    Any class with matching ``ainvoke`` and ``astream`` signatures implicitly
    satisfies this protocol — no explicit inheritance required.

    The chat domain service declares its LLM dependency as
    ``LLMClientProtocol`` rather than the concrete :class:`LLMClient`,
    keeping domain logic free of infrastructure imports.

    Methods
    -------
    ainvoke:
        Invoke the LLM and return the complete AI response as a single
        message.  Suitable for non-streaming endpoints.
    astream:
        Stream the AI response as an async iterator of text chunks.
        Each yielded ``str`` is a non-empty content fragment.  Suitable
        for Server-Sent Events (SSE) endpoints.

    Examples
    --------
    Testing with a mock that satisfies the protocol::

        from unittest.mock import AsyncMock, MagicMock
        from langchain_core.messages.ai import AIMessage

        class MockLLMClient:
            async def ainvoke(self, messages, **kwargs):
                return AIMessage(content="mocked response")

            async def astream(self, messages, **kwargs):
                for chunk in ["Hello", " world"]:
                    yield chunk

        # Structural check passes — no explicit inheritance needed
        assert isinstance(MockLLMClient(), LLMClientProtocol)
    """

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AIMessage:
        """Invoke the LLM and return the full response as an AIMessage.

        Parameters
        ----------
        messages:
            Ordered list of LangChain :class:`~langchain_core.messages.BaseMessage`
            instances representing the conversation history.
        **kwargs:
            Provider-specific options (e.g. ``temperature``, ``max_tokens``,
            ``stop`` sequences) forwarded to the underlying LLM call.

        Returns
        -------
        AIMessage
            The model's complete response.
        """
        ...

    def astream(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream the LLM response as non-empty text chunks.

        Designed for Server-Sent Events (SSE) delivery to the client.
        Empty content chunks (e.g. finish-reason markers) are suppressed
        by the concrete implementation.

        Parameters
        ----------
        messages:
            Ordered list of LangChain :class:`~langchain_core.messages.BaseMessage`
            instances.
        **kwargs:
            Provider-specific options forwarded to the underlying stream call.

        Returns
        -------
        AsyncIterator[str]
            Async iterator yielding non-empty text fragments as they arrive.
        """
        ...


# ---------------------------------------------------------------------------
# LLMClientFactoryProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClientFactoryProtocol(Protocol):
    """Structural interface for factories that produce LLM clients.

    A factory encapsulates the construction logic for :class:`LLMClientProtocol`
    instances (reading settings, wiring credentials, etc.) behind a single
    ``get_llm_client()`` method.

    The chat domain service depends on this factory protocol so that:

    * Production code injects :class:`DefaultLLMClientFactory` (reads from
      application :class:`~app.core.config.Settings`).
    * Tests inject a stub factory that returns a mock client without hitting
      any external LLM provider.

    Any class exposing a ``get_llm_client()`` method that returns an object
    satisfying :class:`LLMClientProtocol` implicitly satisfies this protocol.

    Examples
    --------
    Stub factory for tests::

        class StubLLMClientFactory:
            def get_llm_client(self) -> LLMClientProtocol:
                return MockLLMClient()

        assert isinstance(StubLLMClientFactory(), LLMClientFactoryProtocol)

    FastAPI dependency override pattern::

        from fastapi import Depends
        from domains.chat.llm_client import DefaultLLMClientFactory

        def get_factory() -> LLMClientFactoryProtocol:
            return DefaultLLMClientFactory()

        @router.post("/messages")
        async def send_message(
            factory: LLMClientFactoryProtocol = Depends(get_factory),
        ) -> dict:
            service = ChatService(llm_client=factory.get_llm_client())
            ...
    """

    def get_llm_client(self) -> AbstractLLMPort:
        """Return a fully configured LLM client instance.

        Implementations read provider settings (e.g. from environment
        variables or :class:`~app.core.config.LLMSettings`),
        construct the client, and return it ready to use.

        Returns
        -------
        AbstractLLMPort
            A configured LLM client satisfying :class:`AbstractLLMPort`.
        """
        ...


# ---------------------------------------------------------------------------
# AbstractLLMPort — explicit ABC for LLM port adapters
# ---------------------------------------------------------------------------


class AbstractLLMPort(ABC):
    """Abstract base class that defines the LLM port contract for the chat domain.

    Hexagonal architecture role: this is the **port** — the domain's declaration
    of what it needs from any LLM provider.  Concrete *adapters* (e.g. the
    LangChain + LiteLLM adapter in the infrastructure layer) either inherit
    from this class or satisfy the equivalent :class:`LLMClientProtocol`.

    Two complementary approaches are available in this module:

    * **Protocol (structural subtyping)** — :class:`LLMClientProtocol` checks
      for structural compatibility at runtime via ``isinstance``.  Preferred
      for third-party types that cannot inherit from an ABC.

    * **ABC (nominal subtyping)** — :class:`AbstractLLMPort` (this class)
      requires explicit inheritance and provides ``@abstractmethod`` enforcement
      at class-definition time.  Preferred for first-party adapters where
      explicit contract documentation is desirable.

    Abstract methods
    ----------------
    invoke:
        Non-streaming call — awaits and returns the full LLM response as an
        :class:`~langchain_core.messages.ai.AIMessage`.
    stream:
        Streaming call — returns an :class:`~collections.abc.AsyncIterator`
        that yields non-empty text chunks as they arrive from the provider.

    Usage — implementing a concrete adapter::

        from typing import Any
        from collections.abc import AsyncIterator
        from langchain_core.messages import BaseMessage
        from langchain_core.messages.ai import AIMessage

        from domains.chat.ports import AbstractLLMPort

        class MyLLMAdapter(AbstractLLMPort):
            async def invoke(
                self,
                messages: list[BaseMessage],
                **kwargs: Any,
            ) -> AIMessage:
                # Delegate to the underlying LLM library
                result = await self._backend.ainvoke(messages, **kwargs)
                return result

            async def stream(
                self,
                messages: list[BaseMessage],
                **kwargs: Any,
            ) -> AsyncIterator[str]:
                async for chunk in self._backend.astream(messages, **kwargs):
                    if chunk.content:
                        yield str(chunk.content)

    Usage — type annotation in domain services or factories::

        def build_service(llm: AbstractLLMPort) -> ChatService:
            return ChatService(llm_client=llm)  # type: ignore[arg-type]

    Notes
    -----
    The ``stream`` abstract method signature uses a plain ``def`` with return
    type ``AsyncIterator[str]``.  Concrete implementations may be either:

    * An **async generator** (``async def stream(...): ... yield chunk``) —
      the most common pattern; no explicit return statement is needed.
    * A **regular function** returning any ``AsyncIterator[str]`` — e.g.
      wrapping an existing async iterator from another library.

    ``mypy --strict`` will verify that the returned type is compatible with
    ``AsyncIterator[str]`` at static analysis time.
    """

    @abstractmethod
    async def invoke(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AIMessage:
        """Invoke the LLM and return the complete AI response.

        Concrete implementations must call the underlying LLM provider and
        await the full response before returning.  Suitable for non-streaming
        endpoints where a single complete reply is expected.

        Parameters
        ----------
        messages:
            Ordered conversation history as LangChain
            :class:`~langchain_core.messages.BaseMessage` instances (e.g.
            :class:`~langchain_core.messages.SystemMessage`,
            :class:`~langchain_core.messages.HumanMessage`,
            :class:`~langchain_core.messages.AIMessage` for multi-turn context).
        **kwargs:
            Provider-specific options forwarded verbatim to the underlying
            LLM call.  Common keys: ``temperature: float``,
            ``max_tokens: int``, ``stop: list[str]``.

        Returns
        -------
        AIMessage
            The model's complete response.  The ``content`` attribute holds
            the text; ``response_metadata`` and usage information (when
            exposed by the provider) are also available.

        Raises
        ------
        Exception
            Any provider-level error (network failure, rate limit,
            context-length exceeded, authentication error, etc.) propagates
            to the caller unchanged.  Error translation is the responsibility
            of the calling layer (router or service).

        Examples
        --------
        >>> from langchain_core.messages import HumanMessage
        >>> response = await adapter.invoke([HumanMessage(content="Hello!")])
        >>> print(response.content)
        """
        ...

    @abstractmethod
    def stream(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream the LLM response as non-empty text chunks.

        Concrete implementations must return an async iterator (typically an
        async generator) that yields individual content fragments as they
        arrive from the LLM provider.  Empty fragments (e.g. finish-reason
        markers with no textual content) should be suppressed before yielding.

        Designed for Server-Sent Events (SSE) delivery via
        :class:`sse_starlette.sse.EventSourceResponse`.

        Parameters
        ----------
        messages:
            Ordered conversation history as LangChain
            :class:`~langchain_core.messages.BaseMessage` instances.
        **kwargs:
            Provider-specific options forwarded verbatim to the underlying
            streaming call.  Common keys: ``temperature: float``,
            ``max_tokens: int``, ``stop: list[str]``.

        Returns
        -------
        AsyncIterator[str]
            Async iterator yielding non-empty text fragments in arrival order.
            Each yielded value is a plain ``str`` — callers can forward it
            directly as an SSE ``data`` field.

        Examples
        --------
        Consuming the stream in a FastAPI SSE endpoint::

            from sse_starlette.sse import EventSourceResponse

            @router.post("/stream")
            async def stream_endpoint(
                llm: AbstractLLMPort = Depends(get_llm_port),
            ) -> EventSourceResponse:
                async def _gen():
                    async for chunk in llm.stream(messages):
                        yield {"data": chunk}
                return EventSourceResponse(_gen())

        Implementing as an async generator in a concrete adapter::

            async def stream(
                self,
                messages: list[BaseMessage],
                **kwargs: Any,
            ) -> AsyncIterator[str]:
                async for chunk in self._backend.astream(messages, **kwargs):
                    if chunk.content:
                        yield str(chunk.content)
        """
        ...
