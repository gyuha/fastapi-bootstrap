"""Abstract ports (interfaces) for the chat domain.

Defines the structural contracts that the chat domain relies on, following
hexagonal architecture (Ports & Adapters).  The domain declares *what* it
needs; infrastructure provides the concrete adapters.

Two protocols are defined here:

* :class:`LLMClientProtocol` — the minimal interface any LLM client must
  satisfy: async ``ainvoke`` and ``astream``.  The concrete implementation
  is :class:`~{{ cookiecutter.package_name }}.domains.chat.llm_client.LLMClient`.

* :class:`LLMClientFactoryProtocol` — a factory that exposes a single
  ``get_llm_client()`` method returning an :class:`LLMClientProtocol`.
  The concrete implementation is
  :class:`~{{ cookiecutter.package_name }}.domains.chat.llm_client.DefaultLLMClientFactory`.

The chat domain service (:class:`~{{ cookiecutter.package_name }}.domains.chat.service.ChatService`)
depends *only* on these protocol types, never on concrete implementations.
This enables:

* **Testability** — replace with a mock that satisfies the protocol.
* **Provider portability** — swap the underlying LLM library without
  touching domain logic.
* **Static type safety** — ``mypy`` validates structural compatibility
  at check time via ``typing.Protocol``.

Usage::

    from {{ cookiecutter.package_name }}.domains.chat.ports import (
        LLMClientProtocol,
        LLMClientFactoryProtocol,
    )

    # Type-safe dependency injection in the service
    def build_chat_service(factory: LLMClientFactoryProtocol) -> ChatService:
        return ChatService(llm_client=factory.get_llm_client())

    # Inline structural check (useful in tests)
    from {{ cookiecutter.package_name }}.domains.chat.llm_client import LLMClient
    assert isinstance(LLMClient(...), LLMClientProtocol)
"""

from __future__ import annotations

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
      application :class:`~{{ cookiecutter.package_name }}.core.config.Settings`).
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
        from {{ cookiecutter.package_name }}.domains.chat.llm_client import DefaultLLMClientFactory

        def get_factory() -> LLMClientFactoryProtocol:
            return DefaultLLMClientFactory()

        @router.post("/messages")
        async def send_message(
            factory: LLMClientFactoryProtocol = Depends(get_factory),
        ) -> dict:
            service = ChatService(llm_client=factory.get_llm_client())
            ...
    """

    def get_llm_client(self) -> LLMClientProtocol:
        """Return a fully configured LLM client instance.

        Implementations read provider settings (e.g. from environment
        variables or :class:`~{{ cookiecutter.package_name }}.core.config.LLMSettings`),
        construct the client, and return it ready to use.

        Returns
        -------
        LLMClientProtocol
            A configured LLM client satisfying :class:`LLMClientProtocol`.
        """
        ...
