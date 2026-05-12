"""Tests for the chat domain abstract interfaces (ports).

Verifies that:
* :class:`LLMClientProtocol` is structurally satisfied by :class:`LLMClient`
* :class:`LLMClientFactoryProtocol` is structurally satisfied by
  :class:`DefaultLLMClientFactory`
* Minimal mock classes that implement only the required methods also satisfy
  the protocols (structural/duck-typing confirmation)
* :class:`ChatService` accepts any :class:`AbstractLLMPort` implementor
* :class:`ChatService.complete` delegates to ``llm_client.invoke`` (AbstractLLMPort)
* :class:`ChatService.stream` delegates to ``llm_client.stream`` (AbstractLLMPort)
* :class:`ChatService.__init__` is typed as :class:`AbstractLLMPort` (not LLMClientProtocol)
* The service module has **no runtime LangChain imports** (only TYPE_CHECKING)
* :class:`DefaultLLMClientFactory.get_llm_client` returns an :class:`LLMClient`

All tests are pure unit tests — no network calls, no DB, no Redis.
ChatLiteLLM is mocked throughout to avoid real LLM provider interactions.

Covered scenarios
-----------------
* Protocol isinstance checks for LLMClient and DefaultLLMClientFactory
* Minimal mock satisfies LLMClientProtocol (structural subtyping)
* Minimal mock satisfies LLMClientFactoryProtocol (structural subtyping)
* Non-conforming objects do NOT satisfy the protocols
* ChatService.complete returns AIMessage from llm_client.invoke
* ChatService.stream yields chunks from llm_client.stream
* ChatService typed as AbstractLLMPort (verified via type hints)
* No runtime langchain_core imports in service module namespace
* DefaultLLMClientFactory.get_llm_client delegates to get_settings().llm
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from core.config import LLMProvider, LLMSettings
from domains.chat.llm_client import (
    DefaultLLMClientFactory,
    LLMClient,
    get_llm_client,
)
from domains.chat.ports import (
    AbstractLLMPort,
    LLMClientFactoryProtocol,
    LLMClientProtocol,
)
from domains.chat.service import ChatService

# ---------------------------------------------------------------------------
# Helpers — minimal implementations of the protocols
# ---------------------------------------------------------------------------


class _MinimalLLMClient:
    """Bare-minimum class satisfying both LLMClientProtocol and AbstractLLMPort."""

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content="minimal-response")

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[str]:
        for chunk in ["hello", " world"]:
            yield chunk

    # AbstractLLMPort interface
    async def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content="minimal-response")

    async def stream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[str]:
        for chunk in ["hello", " world"]:
            yield chunk


class _MinimalLLMClientFactory:
    """Bare-minimum class satisfying LLMClientFactoryProtocol — for protocol tests."""

    def get_llm_client(self) -> LLMClientProtocol:
        return _MinimalLLMClient()  # type: ignore[return-value]


class _MissingAinvoke:
    """Object that has astream but NOT ainvoke — should NOT satisfy LLMClientProtocol."""

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[str]:
        yield "chunk"


class _MissingAstream:
    """Object that has ainvoke but NOT astream — should NOT satisfy LLMClientProtocol."""

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content="ok")


class _MissingGetLlmClient:
    """Object without get_llm_client — should NOT satisfy LLMClientFactoryProtocol."""

    def create_client(self) -> LLMClientProtocol:
        return _MinimalLLMClient()  # type: ignore[return-value]


def _make_llm_settings(provider: str = "openai", model: str = "gpt-4o-mini") -> LLMSettings:
    """Construct LLMSettings without touching the environment."""
    return LLMSettings(
        provider=LLMProvider(provider),
        default_model=model,
        OPENAI_API_KEY="sk-test",
    )


def _make_mock_llm_client(response: str = "test response") -> MagicMock:
    """Return a MagicMock whose ainvoke/astream and invoke/stream are properly stubbed."""
    mock = MagicMock(spec=["ainvoke", "astream", "invoke", "stream"])
    mock.ainvoke = AsyncMock(return_value=AIMessage(content=response))
    mock.invoke = AsyncMock(return_value=AIMessage(content=response))

    async def _astream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        for chunk in [response]:
            yield chunk

    async def _stream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
        for chunk in [response]:
            yield chunk

    mock.astream = _astream
    mock.stream = _stream
    return mock


# ---------------------------------------------------------------------------
# LLMClientProtocol — isinstance checks
# ---------------------------------------------------------------------------


class TestLLMClientProtocolStructural:
    """LLMClientProtocol is @runtime_checkable — verify isinstance semantics."""

    def test_llm_client_satisfies_protocol(self) -> None:
        """The concrete LLMClient must satisfy LLMClientProtocol."""
        with patch("infra.llm.provider_factory.ChatLiteLLM"):
            client = LLMClient(settings=_make_llm_settings())
        assert isinstance(client, LLMClientProtocol)

    def test_minimal_implementation_satisfies_protocol(self) -> None:
        """Any class with ainvoke + astream satisfies the protocol."""
        assert isinstance(_MinimalLLMClient(), LLMClientProtocol)

    def test_missing_ainvoke_does_not_satisfy_protocol(self) -> None:
        """Object missing ainvoke must NOT satisfy LLMClientProtocol."""
        assert not isinstance(_MissingAinvoke(), LLMClientProtocol)

    def test_missing_astream_does_not_satisfy_protocol(self) -> None:
        """Object missing astream must NOT satisfy LLMClientProtocol."""
        assert not isinstance(_MissingAstream(), LLMClientProtocol)

    def test_plain_object_does_not_satisfy_protocol(self) -> None:
        """A plain object with no relevant methods must not satisfy the protocol."""
        assert not isinstance(object(), LLMClientProtocol)

    def test_mock_spec_satisfies_protocol(self) -> None:
        """MagicMock(spec=LLMClientProtocol) must satisfy the protocol at runtime."""
        mock = MagicMock(spec=LLMClientProtocol)
        # MagicMock with spec provides the attributes; runtime_checkable checks attribute presence
        assert isinstance(mock, LLMClientProtocol)


# ---------------------------------------------------------------------------
# LLMClientFactoryProtocol — isinstance checks
# ---------------------------------------------------------------------------


class TestLLMClientFactoryProtocolStructural:
    """LLMClientFactoryProtocol is @runtime_checkable — verify isinstance semantics."""

    def test_default_factory_satisfies_protocol(self) -> None:
        """The concrete DefaultLLMClientFactory must satisfy LLMClientFactoryProtocol."""
        assert isinstance(DefaultLLMClientFactory(), LLMClientFactoryProtocol)

    def test_minimal_factory_satisfies_protocol(self) -> None:
        """Any class with get_llm_client() satisfies LLMClientFactoryProtocol."""
        assert isinstance(_MinimalLLMClientFactory(), LLMClientFactoryProtocol)

    def test_missing_get_llm_client_does_not_satisfy(self) -> None:
        """Object without get_llm_client() must NOT satisfy LLMClientFactoryProtocol."""
        assert not isinstance(_MissingGetLlmClient(), LLMClientFactoryProtocol)

    def test_plain_object_does_not_satisfy(self) -> None:
        """A plain object must not satisfy LLMClientFactoryProtocol."""
        assert not isinstance(object(), LLMClientFactoryProtocol)


# ---------------------------------------------------------------------------
# DefaultLLMClientFactory.get_llm_client
# ---------------------------------------------------------------------------


class TestDefaultLLMClientFactory:
    """DefaultLLMClientFactory must return a properly configured LLMClient."""

    def test_returns_llm_client_instance(self) -> None:
        with (
            patch("domains.chat.llm_client.get_settings") as mock_get_settings,
            patch("infra.llm.provider_factory.ChatLiteLLM"),
        ):
            mock_settings = MagicMock()
            mock_settings.llm = _make_llm_settings("openai", "gpt-4o-mini")
            mock_get_settings.return_value = mock_settings

            factory = DefaultLLMClientFactory()
            client = factory.get_llm_client()

        assert isinstance(client, LLMClient)

    def test_returned_client_satisfies_llm_client_protocol(self) -> None:
        """get_llm_client() return value must satisfy LLMClientProtocol."""
        with (
            patch("domains.chat.llm_client.get_settings") as mock_get_settings,
            patch("infra.llm.provider_factory.ChatLiteLLM"),
        ):
            mock_settings = MagicMock()
            mock_settings.llm = _make_llm_settings()
            mock_get_settings.return_value = mock_settings

            client = DefaultLLMClientFactory().get_llm_client()

        assert isinstance(client, LLMClientProtocol)

    def test_get_llm_client_reads_app_settings(self) -> None:
        """Factory must call get_settings() to source LLM configuration."""
        with (
            patch("domains.chat.llm_client.get_settings") as mock_get_settings,
            patch("infra.llm.provider_factory.ChatLiteLLM"),
        ):
            mock_settings = MagicMock()
            mock_settings.llm = _make_llm_settings()
            mock_get_settings.return_value = mock_settings

            DefaultLLMClientFactory().get_llm_client()

        mock_get_settings.assert_called_once()

    def test_factory_satisfies_protocol_at_runtime(self) -> None:
        """DefaultLLMClientFactory() must be an instance of LLMClientFactoryProtocol."""
        assert isinstance(DefaultLLMClientFactory(), LLMClientFactoryProtocol)

    def test_get_llm_client_is_synchronous(self) -> None:
        """get_llm_client must be a plain sync method, not a coroutine."""
        import inspect

        factory = DefaultLLMClientFactory()
        assert not inspect.iscoroutinefunction(factory.get_llm_client), (
            "get_llm_client should be synchronous for use with FastAPI Depends"
        )


# ---------------------------------------------------------------------------
# get_llm_client module-level function delegates to DefaultLLMClientFactory
# ---------------------------------------------------------------------------


class TestGetLlmClientModuleFunction:
    """get_llm_client() module function must delegate to DefaultLLMClientFactory."""

    def test_get_llm_client_returns_llm_client(self) -> None:
        with (
            patch("domains.chat.llm_client.get_settings") as mock_get_settings,
            patch("infra.llm.provider_factory.ChatLiteLLM"),
        ):
            mock_settings = MagicMock()
            mock_settings.llm = _make_llm_settings()
            mock_get_settings.return_value = mock_settings

            client = get_llm_client()

        assert isinstance(client, LLMClient)
        assert isinstance(client, LLMClientProtocol)

    def test_get_llm_client_is_synchronous(self) -> None:
        import inspect

        assert not inspect.iscoroutinefunction(get_llm_client), (
            "get_llm_client module function must be sync for FastAPI Depends"
        )


# ---------------------------------------------------------------------------
# ChatService — depends only on LLMClientProtocol
# ---------------------------------------------------------------------------


class TestChatServiceProtocolDependency:
    """ChatService must accept any LLMClientProtocol implementor."""

    def test_accepts_concrete_llm_client(self) -> None:
        """ChatService can be constructed with the concrete LLMClient."""
        with patch("infra.llm.provider_factory.ChatLiteLLM"):
            client = LLMClient(settings=_make_llm_settings())
        service = ChatService(llm_client=client)
        assert service is not None

    def test_accepts_minimal_mock(self) -> None:
        """ChatService can be constructed with any LLMClientProtocol implementor."""
        service = ChatService(llm_client=_MinimalLLMClient())  # type: ignore[arg-type]
        assert service is not None

    def test_accepts_magic_mock_spec(self) -> None:
        """ChatService can be constructed with MagicMock(spec=LLMClientProtocol)."""
        mock = MagicMock(spec=LLMClientProtocol)
        service = ChatService(llm_client=mock)
        assert service is not None

    def test_internal_llm_type_hint_is_abstract_port(self) -> None:
        """ChatService.__init__ must declare llm_client as AbstractLLMPort.

        Uses typing.get_type_hints() to resolve string annotations produced by
        ``from __future__ import annotations`` (PEP 563).

        The service depends on AbstractLLMPort (the domain's ABC) rather than
        LLMClientProtocol, ensuring zero runtime imports of LangChain/litellm.
        """
        import inspect
        import typing

        sig = inspect.signature(ChatService.__init__)
        llm_param = sig.parameters.get("llm_client")
        assert llm_param is not None, "ChatService.__init__ must have 'llm_client' parameter"

        # Resolve string annotations (PEP 563) back to the actual type objects
        hints = typing.get_type_hints(
            ChatService.__init__,
            localns={"AbstractLLMPort": AbstractLLMPort},
        )
        resolved_annotation = hints.get("llm_client")
        assert resolved_annotation is AbstractLLMPort, (
            f"llm_client parameter should be typed as AbstractLLMPort, got {resolved_annotation!r}"
        )


# ---------------------------------------------------------------------------
# ChatService.complete — delegates to llm_client.invoke (AbstractLLMPort)
# ---------------------------------------------------------------------------


class TestChatServiceComplete:
    """ChatService.complete must delegate to AbstractLLMPort.invoke."""

    @pytest.mark.asyncio
    async def test_complete_returns_ai_message(self) -> None:
        expected = AIMessage(content="The answer is 42.")
        mock_llm = _make_mock_llm_client()
        mock_llm.invoke = AsyncMock(return_value=expected)

        service = ChatService(llm_client=mock_llm)
        result = await service.complete([HumanMessage(content="What is the answer?")])

        assert result is expected
        assert result.content == "The answer is 42."

    @pytest.mark.asyncio
    async def test_complete_calls_invoke_with_messages(self) -> None:
        mock_llm = _make_mock_llm_client()
        messages = [
            SystemMessage(content="Be concise."),
            HumanMessage(content="Capital of France?"),
        ]

        service = ChatService(llm_client=mock_llm)
        await service.complete(messages)

        mock_llm.invoke.assert_awaited_once()
        call_args = mock_llm.invoke.await_args
        assert call_args is not None
        passed_messages = call_args[0][0]
        assert len(passed_messages) == 2

    @pytest.mark.asyncio
    async def test_complete_forwards_kwargs_to_invoke(self) -> None:
        mock_llm = _make_mock_llm_client()
        service = ChatService(llm_client=mock_llm)
        await service.complete([HumanMessage(content="hi")], temperature=0.1, max_tokens=100)

        call_kwargs = mock_llm.invoke.await_args[1]
        assert call_kwargs.get("temperature") == 0.1
        assert call_kwargs.get("max_tokens") == 100

    @pytest.mark.asyncio
    async def test_complete_with_minimal_protocol_impl(self) -> None:
        """Complete must work with any LLMClientProtocol implementation."""
        service = ChatService(llm_client=_MinimalLLMClient())  # type: ignore[arg-type]
        result = await service.complete([HumanMessage(content="test")])
        assert isinstance(result, AIMessage)
        assert result.content == "minimal-response"


# ---------------------------------------------------------------------------
# ChatService.stream — delegates to llm_client.stream (AbstractLLMPort)
# ---------------------------------------------------------------------------


class TestChatServiceStream:
    """ChatService.stream must yield chunks from AbstractLLMPort.stream."""

    @pytest.mark.asyncio
    async def test_stream_yields_chunks_from_llm(self) -> None:
        async def _stream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
            for chunk in ["Hello", ", ", "world", "!"]:
                yield chunk

        mock_llm = MagicMock(spec=["ainvoke", "astream", "invoke", "stream"])
        mock_llm.stream = _stream

        service = ChatService(llm_client=mock_llm)
        collected: list[str] = []
        async for chunk in service.stream([HumanMessage(content="Say hello")]):
            collected.append(chunk)

        assert collected == ["Hello", ", ", "world", "!"]

    @pytest.mark.asyncio
    async def test_stream_with_minimal_protocol_impl(self) -> None:
        """Stream must work with any LLMClientProtocol implementation."""
        service = ChatService(llm_client=_MinimalLLMClient())  # type: ignore[arg-type]
        collected: list[str] = []
        async for chunk in service.stream([HumanMessage(content="test")]):
            collected.append(chunk)

        assert collected == ["hello", " world"]

    @pytest.mark.asyncio
    async def test_stream_yields_strings(self) -> None:
        """All yielded values from ChatService.stream must be str."""
        service = ChatService(llm_client=_MinimalLLMClient())  # type: ignore[arg-type]
        async for chunk in service.stream([HumanMessage(content="test")]):
            assert isinstance(chunk, str), f"Expected str, got {type(chunk)}"

    @pytest.mark.asyncio
    async def test_stream_empty_response(self) -> None:
        """Stream with no chunks from LLM yields nothing."""

        async def _empty_stream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
            for _item in ():
                yield str(_item)

        mock_llm = MagicMock(spec=["ainvoke", "astream", "invoke", "stream"])
        mock_llm.stream = _empty_stream

        service = ChatService(llm_client=mock_llm)
        collected: list[str] = []
        async for chunk in service.stream([HumanMessage(content="test")]):
            collected.append(chunk)

        assert collected == []

    @pytest.mark.asyncio
    async def test_stream_forwards_kwargs_to_stream(self) -> None:
        received_kwargs: dict[str, Any] = {}

        async def _capturing_stream(messages: Any, **kwargs: Any) -> AsyncIterator[str]:
            received_kwargs.update(kwargs)
            yield "chunk"

        mock_llm = MagicMock(spec=["ainvoke", "astream", "invoke", "stream"])
        mock_llm.stream = _capturing_stream

        service = ChatService(llm_client=mock_llm)
        async for _ in service.stream([HumanMessage(content="test")], stop=["<END>"]):
            pass

        assert received_kwargs.get("stop") == ["<END>"]


# ---------------------------------------------------------------------------
# Domain isolation — ChatService does NOT import concrete LLMClient
# ---------------------------------------------------------------------------


class TestChatServiceDomainIsolation:
    """Verify ChatService's module imports don't reference concrete infrastructure."""

    def test_service_module_does_not_import_llm_client_class(self) -> None:
        """The service module must not import the concrete LLMClient."""
        import domains.chat.service as service_module

        # Get all names imported into the service module
        module_vars = vars(service_module)

        # LLMClient (concrete class) must NOT be in the service module namespace
        assert "LLMClient" not in module_vars, (
            "ChatService module imported concrete LLMClient — "
            "it should depend only on AbstractLLMPort from ports.py"
        )

    def test_service_module_does_not_import_langchain_litellm(self) -> None:
        """The service module must not import langchain_litellm (infrastructure)."""
        import domains.chat.service as service_module

        module_vars = vars(service_module)
        assert "ChatLiteLLM" not in module_vars, (
            "ChatService module references ChatLiteLLM — a domain service "
            "must not import infrastructure classes"
        )

    def test_service_module_does_not_import_provider_factory(self) -> None:
        """The service module must not import ProviderFactory (infrastructure)."""
        import domains.chat.service as service_module

        module_vars = vars(service_module)
        assert "ProviderFactory" not in module_vars, (
            "ChatService module references ProviderFactory — a domain service "
            "must not import infrastructure routing classes"
        )

    def test_service_module_has_no_runtime_langchain_imports(self) -> None:
        """The service module must not have langchain types in its runtime namespace.

        LangChain types (BaseMessage, AIMessage) must only appear in the
        TYPE_CHECKING block — never as runtime module-level names.  This
        enforces zero provider imports at service runtime.
        """
        import domains.chat.service as service_module

        module_vars = vars(service_module)
        # These must be absent from the runtime namespace (TYPE_CHECKING only)
        assert "BaseMessage" not in module_vars, (
            "BaseMessage (LangChain type) found in service module runtime namespace — "
            "it must only appear in TYPE_CHECKING block"
        )
        assert "AIMessage" not in module_vars, (
            "AIMessage (LangChain type) found in service module runtime namespace — "
            "it must only appear in TYPE_CHECKING block"
        )

    def test_service_module_references_abstract_llm_port(self) -> None:
        """The service module must reference AbstractLLMPort (not LLMClientProtocol)."""
        import domains.chat.service as service_module
        from domains.chat.ports import AbstractLLMPort

        module_vars = vars(service_module)
        assert "AbstractLLMPort" in module_vars, (
            "AbstractLLMPort must be importable from service module — "
            "it is the domain port that ChatService depends on"
        )
        assert module_vars["AbstractLLMPort"] is AbstractLLMPort
