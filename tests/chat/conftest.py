"""Chat domain pytest conftest — mock infrastructure for LLM provider tests.

Provides reusable fixtures for testing the chat domain without hitting any
real LLM provider endpoint (OpenAI API, local Ollama, etc.).

Architecture
------------
The mock infrastructure intercepts at the :class:`ChatLiteLLM` boundary — the
lowest-level object that would otherwise make network calls.  Everything above
that boundary (``LLMClient``, ``ProviderFactory``, ``ChatService``) is tested
with the real implementation, giving high confidence without live network access.

All mock helpers (``FakeChatLiteLLM``, ``StubLLMClient``, constants) live in
:mod:`tests.chat._mocks` and are imported here.  Test modules that need the
helpers directly should also import from ``_mocks``.

Fixture groups
--------------
**Provider env fixtures** (env var overrides via monkeypatch):

* :func:`env_openai` — sets ``LLM_PROVIDER=openai``, ``LLM_DEFAULT_MODEL``,
  and ``OPENAI_API_KEY`` in the process environment.
* :func:`env_ollama` — sets ``LLM_PROVIDER=ollama``, ``LLM_DEFAULT_MODEL``,
  and ``OLLAMA_BASE_URL``.

**Settings fixtures** (pure :class:`LLMSettings` instances, no env side effects):

* :func:`openai_llm_settings` — :class:`LLMSettings` for OpenAI.
* :func:`ollama_llm_settings` — :class:`LLMSettings` for Ollama.

**ChatLiteLLM patch fixtures** (``unittest.mock.patch``-based):

* :func:`patched_chat_litellm` — patches ``ChatLiteLLM`` with a
  :class:`MagicMock` and yields the mock class.
* :func:`fake_chat_litellm_openai` — patches ``ChatLiteLLM`` with a
  :class:`FakeChatLiteLLM` configured for OpenAI responses.
* :func:`fake_chat_litellm_ollama` — patches ``ChatLiteLLM`` with a
  :class:`FakeChatLiteLLM` configured for Ollama responses.

**LLMClient fixtures** (full ``LLMClient`` objects, patched internally):

* :func:`llm_client_openai` — :class:`LLMClient` for OpenAI.
* :func:`llm_client_ollama` — :class:`LLMClient` for Ollama.

**Mock LLMClient fixtures** (MagicMock-based, for call-count assertions):

* :func:`mock_llm_client` — generic mock with ``ainvoke`` / ``astream`` stubs.
* :func:`mock_llm_client_openai` — mock exposing ``model_string`` / ``provider``.
* :func:`mock_llm_client_ollama` — mock exposing ``model_string`` / ``provider``.

**Stub LLMClient fixtures** (pure Python, zero patches):

* :func:`stub_llm_client` — backed by :class:`StubLLMClient`.
* :func:`streaming_stub_llm_client` — yields :data:`FAKE_STREAM_TOKENS`.

**ChatService fixtures**:

* :func:`stub_chat_service` — fastest option, backed by :func:`stub_llm_client`.
* :func:`chat_service_openai` — backed by :func:`llm_client_openai`.
* :func:`chat_service_ollama` — backed by :func:`llm_client_ollama`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from core.config import LLMProvider, LLMSettings
from domains.chat.ports import LLMClientProtocol
from domains.chat.service import ChatService

from ._mocks import (
    FAKE_RESPONSE_TEXT,
    FAKE_STREAM_TOKENS,
    OLLAMA_DEFAULT_MODEL,
    OLLAMA_TEST_URL,
    OPENAI_DEFAULT_MODEL,
    OPENAI_TEST_KEY,
    FakeChatLiteLLM,
    FakeStreamingChatLiteLLM,
    StubLLMClient,
)

# Re-export so test modules can do:
#   from tests.chat.conftest import FAKE_RESPONSE_TEXT, FakeChatLiteLLM
# (also available via tests.chat._mocks)
__all__ = [
    "FAKE_RESPONSE_TEXT",
    "FAKE_STREAM_TOKENS",
    "OLLAMA_DEFAULT_MODEL",
    "OLLAMA_TEST_URL",
    "OPENAI_DEFAULT_MODEL",
    "OPENAI_TEST_KEY",
    "FakeChatLiteLLM",
    "FakeStreamingChatLiteLLM",
    "StubLLMClient",
]


# ---------------------------------------------------------------------------
# Provider env var fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set OpenAI LLM environment variables for the duration of the test.

    Overrides the following environment variables (restored automatically
    when the test ends)::

        LLM_PROVIDER        = openai
        LLM_DEFAULT_MODEL   = gpt-4o-mini
        OPENAI_API_KEY      = sk-test-openai-key-for-unit-tests

    The ``get_settings`` LRU cache is cleared by the root
    ``settings_cache_clear`` autouse fixture before each test so the
    new env vars are picked up on the next :func:`get_settings` call.

    Usage::

        def test_with_openai_env(env_openai):
            from core.config import get_settings
            s = get_settings()
            assert s.llm_provider.value == "openai"
    """
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_DEFAULT_MODEL)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)


@pytest.fixture
def env_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set Ollama LLM environment variables for the duration of the test.

    Overrides the following environment variables (restored automatically
    when the test ends)::

        LLM_PROVIDER        = ollama
        LLM_DEFAULT_MODEL   = llama3.2
        OLLAMA_BASE_URL     = http://localhost:11434

    No ``OPENAI_API_KEY`` or other cloud-provider key is set, mirroring the
    real Ollama workflow where no API key is required.

    Usage::

        def test_with_ollama_env(env_ollama):
            from core.config import get_settings
            s = get_settings()
            assert s.llm_provider.value == "ollama"
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL)
    monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# LLMSettings fixtures (pure objects, no env side effects)
# ---------------------------------------------------------------------------


@pytest.fixture
def openai_llm_settings() -> LLMSettings:
    """Return an :class:`LLMSettings` instance pre-configured for OpenAI.

    Constructed directly (not from env vars) — hermetic by design.

    Returns
    -------
    LLMSettings
        ``provider=openai``, ``default_model=gpt-4o-mini``, test API key set.
    """
    return LLMSettings(
        provider=LLMProvider.openai,
        default_model=OPENAI_DEFAULT_MODEL,
        OPENAI_API_KEY=OPENAI_TEST_KEY,
    )


@pytest.fixture
def ollama_llm_settings() -> LLMSettings:
    """Return an :class:`LLMSettings` instance pre-configured for Ollama.

    Constructed directly — no API key needed (Ollama does not require one).

    Returns
    -------
    LLMSettings
        ``provider=ollama``, ``default_model=llama3.2``,
        ``ollama_base_url=http://localhost:11434``.
    """
    return LLMSettings(
        provider=LLMProvider.ollama,
        default_model=OLLAMA_DEFAULT_MODEL,
        OLLAMA_BASE_URL=OLLAMA_TEST_URL,
    )


# ---------------------------------------------------------------------------
# ChatLiteLLM patch fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_chat_litellm() -> Any:  # type: ignore[misc]
    """Patch :class:`ChatLiteLLM` with a :class:`MagicMock` for the test duration.

    Patches the import in the ``llm_client`` module (where
    ``LLMClient.__init__`` calls ``ChatLiteLLM(**kwargs)``).

    Yields
    ------
    MagicMock
        The mock *class*.  Use ``patched_chat_litellm.return_value`` to access
        the instance that ``LLMClient.__init__`` will hold.

    Usage::

        def test_kwargs_forwarded(patched_chat_litellm, openai_llm_settings):
            from domains.chat.llm_client import LLMClient
            LLMClient(settings=openai_llm_settings, temperature=0.7)
            _, kwargs = patched_chat_litellm.call_args
            assert kwargs["temperature"] == 0.7
            assert kwargs["model"] == "openai/gpt-4o-mini"
    """
    with patch("infra.llm.provider_factory.ChatLiteLLM") as mock_cls:
        yield mock_cls


@pytest.fixture
def fake_chat_litellm_openai() -> Any:  # type: ignore[misc]
    """Patch :class:`ChatLiteLLM` with a :class:`FakeChatLiteLLM` for OpenAI.

    The fake instance's ``ainvoke`` / ``astream`` return configurable
    pre-canned responses — no network call is ever made.

    Yields
    ------
    FakeChatLiteLLM
        The fake instance injected into :class:`LLMClient`.
    """
    fake_instance = FakeChatLiteLLM(
        response=FAKE_RESPONSE_TEXT,
        model=f"openai/{OPENAI_DEFAULT_MODEL}",
        api_key=OPENAI_TEST_KEY,
    )
    with patch(
        "infra.llm.provider_factory.ChatLiteLLM",
        return_value=fake_instance,
    ):
        yield fake_instance


@pytest.fixture
def fake_chat_litellm_ollama() -> Any:  # type: ignore[misc]
    """Patch :class:`ChatLiteLLM` with a :class:`FakeChatLiteLLM` for Ollama.

    The fake instance is pre-configured with Ollama-specific kwargs (``api_base``,
    sentinel ``api_key="ollama"``) so introspection tests can verify routing.

    Yields
    ------
    FakeChatLiteLLM
        The fake instance injected into :class:`LLMClient`.
    """
    fake_instance = FakeChatLiteLLM(
        response=FAKE_RESPONSE_TEXT,
        model=f"ollama/{OLLAMA_DEFAULT_MODEL}",
        api_base=OLLAMA_TEST_URL,
        api_key="ollama",
    )
    with patch(
        "infra.llm.provider_factory.ChatLiteLLM",
        return_value=fake_instance,
    ):
        yield fake_instance


# ---------------------------------------------------------------------------
# LLMClient fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def llm_client_openai(
    openai_llm_settings: LLMSettings,
    fake_chat_litellm_openai: FakeChatLiteLLM,
) -> Any:
    """Return a configured :class:`LLMClient` for OpenAI with no network I/O.

    ``ChatLiteLLM`` is replaced by the :func:`fake_chat_litellm_openai` fake,
    so all ``ainvoke`` / ``astream`` calls return pre-canned responses.

    Returns
    -------
    LLMClient
        ``provider="openai"``, ``model_string="openai/gpt-4o-mini"``.

    Usage::

        def test_model_string(llm_client_openai):
            assert llm_client_openai.model_string == "openai/gpt-4o-mini"

        async def test_ainvoke(llm_client_openai):
            from langchain_core.messages import HumanMessage
            result = await llm_client_openai.ainvoke([HumanMessage(content="hello")])
            assert result.content == FAKE_RESPONSE_TEXT
    """
    # ChatLiteLLM is already patched by fake_chat_litellm_openai fixture
    from domains.chat.llm_client import LLMClient

    return LLMClient(settings=openai_llm_settings)


@pytest.fixture
def llm_client_ollama(
    ollama_llm_settings: LLMSettings,
    fake_chat_litellm_ollama: FakeChatLiteLLM,
) -> Any:
    """Return a configured :class:`LLMClient` for Ollama with no network I/O.

    ``ChatLiteLLM`` is replaced by the :func:`fake_chat_litellm_ollama` fake.

    Returns
    -------
    LLMClient
        ``provider="ollama"``, ``model_string="ollama/llama3.2"``.

    Usage::

        def test_model_string(llm_client_ollama):
            assert llm_client_ollama.model_string == "ollama/llama3.2"

        async def test_astream(llm_client_ollama):
            from langchain_core.messages import HumanMessage
            chunks = [c async for c in llm_client_ollama.astream([HumanMessage(content="hi")])]
            assert len(chunks) > 0
    """
    from domains.chat.llm_client import LLMClient

    return LLMClient(settings=ollama_llm_settings)


# ---------------------------------------------------------------------------
# MagicMock-based LLMClient fixtures (for call-count / call-arg assertions)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """Return a :class:`MagicMock` satisfying both :class:`LLMClientProtocol` and :class:`AbstractLLMPort`.

    Use when you need to assert on call counts or capture arguments passed
    to the LLM client — typically in :class:`ChatService` tests.

    Returns
    -------
    MagicMock
        Mock with ``ainvoke``/``invoke`` (:class:`AsyncMock`) and
        ``astream``/``stream`` (async generators).

    Usage::

        async def test_service_calls_invoke_once(mock_llm_client):
            from langchain_core.messages import HumanMessage
            service = ChatService(llm_client=mock_llm_client)
            await service.complete([HumanMessage(content="hi")])
            mock_llm_client.invoke.assert_awaited_once()
    """
    mock = MagicMock(spec=["ainvoke", "astream", "invoke", "stream"])
    mock.ainvoke = AsyncMock(return_value=AIMessage(content=FAKE_RESPONSE_TEXT))
    mock.invoke = AsyncMock(return_value=AIMessage(content=FAKE_RESPONSE_TEXT))

    async def _astream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        for token in FAKE_STREAM_TOKENS:
            yield token

    async def _stream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        for token in FAKE_STREAM_TOKENS:
            yield token

    mock.astream = _astream
    mock.stream = _stream
    return mock


@pytest.fixture
def mock_llm_client_openai() -> MagicMock:
    """Return a :class:`MagicMock` satisfying both protocols, wired for OpenAI.

    Adds ``model_string`` and ``provider`` attributes for tests that assert
    on provider-specific routing properties.

    Returns
    -------
    MagicMock
        Mock with ``model_string="openai/gpt-4o-mini"``, ``provider="openai"``.
    """
    mock = MagicMock(spec=["ainvoke", "astream", "invoke", "stream", "model_string", "provider"])
    mock.model_string = f"openai/{OPENAI_DEFAULT_MODEL}"
    mock.provider = "openai"
    mock.ainvoke = AsyncMock(return_value=AIMessage(content=FAKE_RESPONSE_TEXT))
    mock.invoke = AsyncMock(return_value=AIMessage(content=FAKE_RESPONSE_TEXT))

    async def _astream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        for token in FAKE_STREAM_TOKENS:
            yield token

    async def _stream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        for token in FAKE_STREAM_TOKENS:
            yield token

    mock.astream = _astream
    mock.stream = _stream
    return mock


@pytest.fixture
def mock_llm_client_ollama() -> MagicMock:
    """Return a :class:`MagicMock` satisfying both protocols, wired for Ollama.

    Adds ``model_string`` and ``provider`` attributes for tests that assert
    on Ollama-specific routing properties.

    Returns
    -------
    MagicMock
        Mock with ``model_string="ollama/llama3.2"``, ``provider="ollama"``.
    """
    mock = MagicMock(spec=["ainvoke", "astream", "invoke", "stream", "model_string", "provider"])
    mock.model_string = f"ollama/{OLLAMA_DEFAULT_MODEL}"
    mock.provider = "ollama"
    mock.ainvoke = AsyncMock(return_value=AIMessage(content=FAKE_RESPONSE_TEXT))
    mock.invoke = AsyncMock(return_value=AIMessage(content=FAKE_RESPONSE_TEXT))

    async def _astream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        for token in FAKE_STREAM_TOKENS:
            yield token

    async def _stream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        for token in FAKE_STREAM_TOKENS:
            yield token

    mock.astream = _astream
    mock.stream = _stream
    return mock


# ---------------------------------------------------------------------------
# Stub LLMClient fixtures (pure Python, zero patches)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_llm_client() -> LLMClientProtocol:
    """Return a minimal stub satisfying :class:`LLMClientProtocol` (no patches).

    The fastest and simplest LLM client fixture — zero ``unittest.mock.patch``
    overhead.  Suitable for tests that exercise service-level logic without
    caring about which concrete LLM client is used.

    Returns
    -------
    LLMClientProtocol
        An object satisfying :class:`LLMClientProtocol` structurally.

    Usage::

        async def test_complete_returns_content(stub_llm_client):
            from langchain_core.messages import HumanMessage
            service = ChatService(llm_client=stub_llm_client)
            result = await service.complete([HumanMessage(content="hi")])
            assert result.content == FAKE_RESPONSE_TEXT
    """
    return StubLLMClient(response=FAKE_RESPONSE_TEXT)  # type: ignore[return-value]


@pytest.fixture
def streaming_stub_llm_client() -> LLMClientProtocol:
    """Return a stub :class:`LLMClientProtocol` that yields :data:`FAKE_STREAM_TOKENS`.

    Suitable for tests that verify streaming end-to-end (multi-chunk paths).

    Returns
    -------
    LLMClientProtocol
        Stub that yields each token from :data:`FAKE_STREAM_TOKENS` in order.

    Usage::

        async def test_stream_all_tokens(streaming_stub_llm_client):
            from langchain_core.messages import HumanMessage
            service = ChatService(llm_client=streaming_stub_llm_client)
            chunks = [c async for c in service.stream([HumanMessage(content="hi")])]
            assert chunks == FAKE_STREAM_TOKENS
    """
    return StubLLMClient(  # type: ignore[return-value]
        response="".join(FAKE_STREAM_TOKENS),
        stream_tokens=list(FAKE_STREAM_TOKENS),
    )


# ---------------------------------------------------------------------------
# ChatService fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_chat_service(stub_llm_client: LLMClientProtocol) -> ChatService:
    """Return a :class:`ChatService` backed by :func:`stub_llm_client`.

    The fastest service fixture — no patches, no network, pure Python.

    Returns
    -------
    ChatService
        Service with an injected :class:`StubLLMClient`.

    Usage::

        async def test_complete(stub_chat_service):
            from langchain_core.messages import HumanMessage
            result = await stub_chat_service.complete([HumanMessage(content="hi")])
            assert result.content == FAKE_RESPONSE_TEXT
    """
    return ChatService(llm_client=stub_llm_client)


@pytest.fixture
def chat_service_openai(llm_client_openai: Any) -> ChatService:
    """Return a :class:`ChatService` backed by :func:`llm_client_openai`.

    Uses the real ``LLMClient`` implementation with ``ChatLiteLLM`` patched
    by :class:`FakeChatLiteLLM`.

    Returns
    -------
    ChatService
        Service configured to use the fake OpenAI LLM client.
    """
    return ChatService(llm_client=llm_client_openai)


@pytest.fixture
def chat_service_ollama(llm_client_ollama: Any) -> ChatService:
    """Return a :class:`ChatService` backed by :func:`llm_client_ollama`.

    Uses the real ``LLMClient`` implementation with ``ChatLiteLLM`` patched
    by :class:`FakeChatLiteLLM`.

    Returns
    -------
    ChatService
        Service configured to use the fake Ollama LLM client.
    """
    return ChatService(llm_client=llm_client_ollama)
