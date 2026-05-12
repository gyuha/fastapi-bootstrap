"""Shared test doubles for the chat domain.

This module contains re-usable mock/fake classes and constants used across
the chat test suite.  It is imported by both ``conftest.py`` (fixtures) and
individual test modules (assertions / introspection).

Keeping mock definitions here — rather than inside conftest.py — avoids
confusion between pytest fixtures (``@pytest.fixture``) and plain helper
classes, and makes the doubles importable without pytest magic.

Classes
-------
* :class:`FakeChatLiteLLM` — async drop-in for ``ChatLiteLLM`` that returns
  configurable pre-canned responses without any network call.
* :class:`FakeStreamingChatLiteLLM` — variant of :class:`FakeChatLiteLLM`
  with richer default streaming behaviour.
* :class:`StubLLMClient` — minimal :class:`LLMClientProtocol` satisfier
  backed by a :class:`FakeChatLiteLLM`; no ``unittest.mock.patch`` needed.

Constants
---------
* :data:`OPENAI_TEST_KEY` — fake OpenAI API key sentinel.
* :data:`OLLAMA_TEST_URL` — fake Ollama base URL.
* :data:`OPENAI_DEFAULT_MODEL` — default OpenAI model name used in fixtures.
* :data:`OLLAMA_DEFAULT_MODEL` — default Ollama model name used in fixtures.
* :data:`FAKE_RESPONSE_TEXT` — default text returned by :meth:`FakeChatLiteLLM.ainvoke`.
* :data:`FAKE_STREAM_TOKENS` — default token list yielded by :meth:`FakeChatLiteLLM.astream`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import AIMessageChunk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fake OpenAI API key used in tests — never validated against the real API.
OPENAI_TEST_KEY: str = "sk-test-openai-key-for-unit-tests"

#: Fake Ollama base URL used in tests — never connects to a real Ollama server.
OLLAMA_TEST_URL: str = "http://localhost:11434"

#: Default model name for the OpenAI provider fixtures.
OPENAI_DEFAULT_MODEL: str = "gpt-4o-mini"

#: Default model name for the Ollama provider fixtures.
OLLAMA_DEFAULT_MODEL: str = "llama3.2"

#: Default canned response text returned by :class:`FakeChatLiteLLM` ainvoke.
FAKE_RESPONSE_TEXT: str = "This is a fake LLM response for testing."

#: Default token sequence yielded by :class:`FakeChatLiteLLM` astream.
FAKE_STREAM_TOKENS: list[str] = ["This ", "is ", "a ", "streamed ", "response."]


# ---------------------------------------------------------------------------
# FakeChatLiteLLM — async drop-in for ChatLiteLLM
# ---------------------------------------------------------------------------


class FakeChatLiteLLM:
    """Synchronous/async test double for :class:`langchain_litellm.ChatLiteLLM`.

    Accepts the same ``**kwargs`` as ``ChatLiteLLM`` but never makes any
    network call.  Configured responses and captured constructor kwargs are
    available as instance attributes for introspection.

    Parameters
    ----------
    response:
        The text content returned by :meth:`ainvoke`.  Defaults to
        :data:`FAKE_RESPONSE_TEXT`.
    stream_tokens:
        List of string tokens yielded by :meth:`astream`.  Defaults to
        :data:`FAKE_STREAM_TOKENS`.
    **kwargs:
        Forwarded constructor arguments (``model``, ``api_key``, etc.) —
        stored as :attr:`init_kwargs` for introspection in tests.

    Attributes
    ----------
    init_kwargs : dict[str, Any]
        All keyword arguments passed to ``__init__`` (excluding ``response``
        and ``stream_tokens``).  Introspect in tests to verify provider-specific
        routing kwargs (``model``, ``api_key``, ``api_base``, etc.).
    ainvoke_call_count : int
        Number of times :meth:`ainvoke` has been called.
    astream_call_count : int
        Number of times :meth:`astream` has been called.
    last_messages : list[BaseMessage]
        The most recent message list passed to :meth:`ainvoke` or :meth:`astream`.
    last_kwargs : dict[str, Any]
        The most recent extra kwargs passed to :meth:`ainvoke` or :meth:`astream`.

    Examples
    --------
    Use as a ``ChatLiteLLM`` replacement::

        fake = FakeChatLiteLLM(response="hello world", model="openai/gpt-4o")
        result = await fake.ainvoke([HumanMessage(content="hi")])
        assert result.content == "hello world"

    Stream tokens::

        chunks = [c async for c in fake.astream([HumanMessage(content="hi")])]
        assert [c.content for c in chunks] == FAKE_STREAM_TOKENS

    Introspect constructor kwargs::

        assert fake.init_kwargs["model"] == "openai/gpt-4o"

    Verify call counts::

        await fake.ainvoke([HumanMessage(content="first call")])
        assert fake.ainvoke_call_count == 1
    """

    def __init__(
        self,
        response: str = FAKE_RESPONSE_TEXT,
        stream_tokens: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.response: str = response
        self.stream_tokens: list[str] = (
            stream_tokens if stream_tokens is not None else list(FAKE_STREAM_TOKENS)
        )
        self.init_kwargs: dict[str, Any] = kwargs
        self.ainvoke_call_count: int = 0
        self.astream_call_count: int = 0
        self.last_messages: list[BaseMessage] = []
        self.last_kwargs: dict[str, Any] = {}

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AIMessage:
        """Return a pre-canned :class:`AIMessage` without network I/O."""
        self.ainvoke_call_count += 1
        self.last_messages = list(messages)
        self.last_kwargs = dict(kwargs)
        return AIMessage(content=self.response)

    async def astream(  # type: ignore[override]
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """Yield pre-canned :class:`AIMessageChunk` tokens without network I/O.

        Each token in :attr:`stream_tokens` is yielded as an
        :class:`AIMessageChunk` so that the caller (``LLMClient.astream``)
        can extract ``.content`` and filter empty chunks exactly as it would
        with a real ``ChatLiteLLM``.
        """
        self.astream_call_count += 1
        self.last_messages = list(messages)
        self.last_kwargs = dict(kwargs)
        for token in self.stream_tokens:
            yield AIMessageChunk(content=token)


# ---------------------------------------------------------------------------
# FakeStreamingChatLiteLLM — character-level streaming variant
# ---------------------------------------------------------------------------


class FakeStreamingChatLiteLLM(FakeChatLiteLLM):
    """Variant of :class:`FakeChatLiteLLM` with optional character-level tokens.

    Behaves identically to :class:`FakeChatLiteLLM` but adds a ``token_count``
    parameter that splits the response into smaller chunks.  Use when you need
    to exercise multi-chunk streaming code paths more thoroughly.

    Parameters
    ----------
    response:
        Full text returned by ``ainvoke`` (joined tokens from ``astream``).
    token_count:
        When set, ``stream_tokens`` is derived by splitting ``response``
        into at most ``token_count`` sub-strings.  When ``None`` (default),
        uses :data:`FAKE_STREAM_TOKENS`.
    **kwargs:
        Forwarded to :class:`FakeChatLiteLLM`.
    """

    def __init__(
        self,
        response: str = FAKE_RESPONSE_TEXT,
        token_count: int | None = None,
        **kwargs: Any,
    ) -> None:
        if token_count is not None and len(response) > 0:
            chunk_size = max(1, len(response) // token_count)
            tokens = [response[i : i + chunk_size] for i in range(0, len(response), chunk_size)]
        else:
            tokens = None  # falls back to FAKE_STREAM_TOKENS in parent
        super().__init__(response=response, stream_tokens=tokens, **kwargs)


# ---------------------------------------------------------------------------
# StubLLMClient — minimal LLMClientProtocol satisfier, no patches needed
# ---------------------------------------------------------------------------


class StubLLMClient:
    """Minimal :class:`~app.domains.chat.ports.LLMClientProtocol` satisfier.

    A plain Python class — no ``unittest.mock.patch`` is used.  This makes it
    the *fastest* and *simplest* way to inject an LLM client into
    :class:`~app.domains.chat.service.ChatService` during tests.

    Satisfies the protocol structurally: has ``ainvoke`` and ``astream`` with
    the correct signatures.  Use when you need to test service-level logic
    without any patching overhead.

    Parameters
    ----------
    response:
        Text content returned by :meth:`ainvoke` and joined stream from
        :meth:`astream`.  Defaults to :data:`FAKE_RESPONSE_TEXT`.
    stream_tokens:
        Tokens yielded by :meth:`astream`.  Defaults to
        :data:`FAKE_STREAM_TOKENS`.

    Examples
    --------
    ::

        from langchain_core.messages import HumanMessage
        from domains.chat.service import ChatService

        client = StubLLMClient()
        service = ChatService(llm_client=client)  # type: ignore[arg-type]
        result = await service.complete([HumanMessage(content="hi")])
        assert result.content == FAKE_RESPONSE_TEXT
    """

    def __init__(
        self,
        response: str = FAKE_RESPONSE_TEXT,
        stream_tokens: list[str] | None = None,
    ) -> None:
        self._response: str = response
        self._stream_tokens: list[str] = (
            stream_tokens if stream_tokens is not None else list(FAKE_STREAM_TOKENS)
        )

    async def ainvoke(
        self,
        messages: list[Any],
        **kwargs: Any,
    ) -> AIMessage:
        """Return a pre-canned :class:`AIMessage` (LLMClientProtocol interface)."""
        return AIMessage(content=self._response)

    async def astream(
        self,
        messages: list[Any],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Yield pre-canned string tokens (LLMClientProtocol interface)."""
        for token in self._stream_tokens:
            yield token

    # ------------------------------------------------------------------
    # AbstractLLMPort interface (invoke / stream)
    # ------------------------------------------------------------------

    async def invoke(
        self,
        messages: list[Any],
        **kwargs: Any,
    ) -> AIMessage:
        """Return a pre-canned :class:`AIMessage` (AbstractLLMPort interface)."""
        return AIMessage(content=self._response)

    async def stream(
        self,
        messages: list[Any],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Yield pre-canned string tokens (AbstractLLMPort interface)."""
        for token in self._stream_tokens:
            yield token
