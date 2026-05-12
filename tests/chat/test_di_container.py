"""Unit tests for the chat domain DI container.

Verifies that :mod:`fastapi_bootstrap.domains.chat.container` correctly:

* Binds :class:`~fastapi_bootstrap.domains.chat.llm_client.DefaultLLMClientFactory`
  to :class:`LLMClientFactoryProtocol` (interface) at runtime.
* :func:`get_llm_factory` returns an object satisfying
  :class:`LLMClientFactoryProtocol` — *not* the concrete class by name.
* :func:`get_chat_service` factory parameter is typed as
  :class:`LLMClientFactoryProtocol` (interface), not ``DefaultLLMClientFactory``.
* The ``container`` module namespace is free of concrete infrastructure symbols
  (``ChatLiteLLM``, ``DefaultLLMClientFactory``, ``LLMClient``, ``ProviderFactory``)
  — the concrete factory is imported lazily inside :func:`get_llm_factory`.
* A stub factory (pure interface implementation, zero concrete imports) wires
  correctly through :func:`get_chat_service` and produces a working
  :class:`ChatService`.
* The :class:`ChatService` returned by the container holds an
  :class:`LLMClientProtocol` reference — never a factory or a concrete client.

All tests are **pure unit tests** — no network calls, no DB, no Redis.

Covered scenarios
-----------------
* Module-level namespace isolation (no concrete infra exposed)
* get_llm_factory return type satisfies the protocol
* get_llm_factory return type annotation is the interface, not concrete
* get_chat_service factory parameter annotation is LLMClientFactoryProtocol
* Stub factory works end-to-end through get_chat_service
* MagicMock factory works end-to-end (verifies duck-typing)
* ChatService produced by DI container can complete() and stream()
* service._llm satisfies LLMClientProtocol
* ChatService.service module has no concrete provider imports (regression)
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from domains.chat.container import get_chat_service, get_llm_factory
from domains.chat.ports import (
    AbstractLLMPort,
    LLMClientFactoryProtocol,
    LLMClientProtocol,
)
from domains.chat.service import ChatService

# ---------------------------------------------------------------------------
# Stub implementations — zero concrete provider imports
# ---------------------------------------------------------------------------


class _StubLLMClient:
    """Satisfies both :class:`LLMClientProtocol` and :class:`AbstractLLMPort`.

    No concrete infrastructure imports — pure Python stub for DI tests.
    """

    def __init__(self, response: str = "stub-response") -> None:
        self._response = response

    # LLMClientProtocol interface
    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content=self._response)

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[str]:
        yield self._response

    # AbstractLLMPort interface (used by ChatService)
    async def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content=self._response)

    async def stream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[str]:
        yield self._response


class _StubLLMClientFactory:
    """Minimal :class:`LLMClientFactoryProtocol` satisfier — no concrete infra imports."""

    def __init__(self, response: str = "stub-response") -> None:
        self._response = response

    def get_llm_client(self) -> LLMClientProtocol:
        return _StubLLMClient(self._response)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Container module isolation — no concrete infra in module namespace
# ---------------------------------------------------------------------------


class TestContainerModuleIsolation:
    """The container module must NOT expose concrete infrastructure at module level.

    This test class constitutes the primary *domain isolation* guard: it verifies
    that importing :mod:`container` does not pull concrete provider classes into
    the caller's namespace.  The lazy import inside :func:`get_llm_factory` is
    what makes this possible.
    """

    def test_container_does_not_expose_chat_litellm(self) -> None:
        """``ChatLiteLLM`` must NOT appear in the container module namespace."""
        import domains.chat.container as container_module

        assert "ChatLiteLLM" not in vars(container_module), (
            "container.py exposed ChatLiteLLM at module level — concrete "
            "infrastructure must not leak into the container namespace"
        )

    def test_container_does_not_expose_default_factory(self) -> None:
        """``DefaultLLMClientFactory`` must NOT appear in the container namespace.

        The concrete factory is imported lazily *inside* :func:`get_llm_factory`
        so that the container module's namespace remains free of it.
        """
        import domains.chat.container as container_module

        assert "DefaultLLMClientFactory" not in vars(container_module), (
            "container.py exposed DefaultLLMClientFactory at module level — "
            "the concrete factory should only be imported inside get_llm_factory()"
        )

    def test_container_does_not_expose_llm_client_class(self) -> None:
        """``LLMClient`` (concrete client) must NOT appear in the container namespace."""
        import domains.chat.container as container_module

        assert "LLMClient" not in vars(container_module), (
            "container.py exposed concrete LLMClient class at module level"
        )

    def test_container_does_not_expose_provider_factory(self) -> None:
        """``ProviderFactory`` (routing helper) must NOT appear in the container namespace."""
        import domains.chat.container as container_module

        assert "ProviderFactory" not in vars(container_module), (
            "container.py exposed ProviderFactory at module level — routing "
            "infrastructure must not leak into the DI container namespace"
        )

    def test_container_does_not_expose_langchain_litellm(self) -> None:
        """``langchain_litellm`` package must NOT be in the container namespace."""
        import domains.chat.container as container_module

        assert "langchain_litellm" not in vars(container_module), (
            "container.py imported langchain_litellm — a DI container must "
            "not reference infrastructure library packages at module level"
        )

    def test_container_public_api_is_only_di_functions_and_interface(self) -> None:
        """Only DI functions and interface types should be publicly accessible."""
        import domains.chat.container as container_module

        # Concrete infrastructure names that must NOT be present
        forbidden = {
            "ChatLiteLLM",
            "DefaultLLMClientFactory",
            "LLMClient",
            "ProviderFactory",
            "langchain_litellm",
        }
        module_vars = vars(container_module)
        for name in forbidden:
            assert name not in module_vars, (
                f"container.py exposed forbidden concrete symbol {name!r} at "
                "module level — it must remain behind a lazy import"
            )


# ---------------------------------------------------------------------------
# get_llm_factory — interface binding verification
# ---------------------------------------------------------------------------


class TestGetLlmFactory:
    """get_llm_factory() must return an object satisfying LLMClientFactoryProtocol."""

    def test_return_value_satisfies_protocol(self) -> None:
        """The registered factory must be an instance of LLMClientFactoryProtocol."""
        with (
            patch("domains.chat.llm_client.get_settings") as mock_settings,
            patch("infra.llm.provider_factory.ChatLiteLLM"),
        ):
            mock_settings.return_value.llm.provider.value = "openai"
            factory = get_llm_factory()

        assert isinstance(factory, LLMClientFactoryProtocol), (
            "get_llm_factory() must return an object satisfying LLMClientFactoryProtocol"
        )

    def test_return_annotation_is_interface_not_concrete(self) -> None:
        """The return type annotation of get_llm_factory must be the interface type.

        Uses :func:`typing.get_type_hints` to resolve PEP 563 string annotations
        back to the actual type objects.
        """
        hints = typing.get_type_hints(get_llm_factory)
        return_hint = hints.get("return")

        assert return_hint is LLMClientFactoryProtocol, (
            f"get_llm_factory return annotation must be LLMClientFactoryProtocol, "
            f"got {return_hint!r}. The function must be typed against the interface, "
            f"not the concrete DefaultLLMClientFactory."
        )

    def test_factory_has_get_llm_client_callable(self) -> None:
        """The returned factory must expose a callable get_llm_client."""
        with (
            patch("domains.chat.llm_client.get_settings") as mock_settings,
            patch("infra.llm.provider_factory.ChatLiteLLM"),
        ):
            mock_settings.return_value.llm.provider.value = "openai"
            factory = get_llm_factory()

        assert hasattr(factory, "get_llm_client"), (
            "Factory from get_llm_factory() must have a 'get_llm_client' attribute"
        )
        assert callable(factory.get_llm_client), "factory.get_llm_client must be callable"

    def test_is_synchronous(self) -> None:
        """get_llm_factory must be synchronous (not a coroutine) for FastAPI Depends."""
        assert not inspect.iscoroutinefunction(get_llm_factory), (
            "get_llm_factory must be sync so FastAPI can call it without await"
        )

    def test_stub_factory_satisfies_same_protocol(self) -> None:
        """A stub factory with no concrete imports must satisfy LLMClientFactoryProtocol.

        This demonstrates that the protocol contract is achievable without
        any concrete infrastructure dependency.
        """
        stub = _StubLLMClientFactory()
        assert isinstance(stub, LLMClientFactoryProtocol), (
            "_StubLLMClientFactory (no concrete imports) must satisfy "
            "LLMClientFactoryProtocol — the protocol is purely structural"
        )

    def test_mock_factory_satisfies_protocol(self) -> None:
        """MagicMock(spec=LLMClientFactoryProtocol) satisfies the protocol."""
        mock_factory = MagicMock(spec=LLMClientFactoryProtocol)
        assert isinstance(mock_factory, LLMClientFactoryProtocol)


# ---------------------------------------------------------------------------
# get_chat_service — DI wiring and type annotation tests
# ---------------------------------------------------------------------------


class TestGetChatService:
    """get_chat_service() must wire ChatService using the factory interface."""

    def test_factory_param_annotation_is_interface(self) -> None:
        """The 'factory' parameter annotation must be LLMClientFactoryProtocol.

        This verifies the service builder is typed against the interface, not
        the concrete class — the core requirement of Sub-AC 2.3.
        """
        hints = typing.get_type_hints(get_chat_service)
        factory_hint = hints.get("factory")

        assert factory_hint is LLMClientFactoryProtocol, (
            f"get_chat_service 'factory' parameter must be typed as "
            f"LLMClientFactoryProtocol, got {factory_hint!r}. "
            f"The DI function must declare the interface, not the concrete class."
        )

    def test_return_annotation_is_chat_service(self) -> None:
        """The return annotation of get_chat_service must be ChatService."""
        hints = typing.get_type_hints(get_chat_service)
        return_hint = hints.get("return")

        assert return_hint is ChatService, (
            f"get_chat_service return annotation must be ChatService, got {return_hint!r}"
        )

    def test_is_synchronous(self) -> None:
        """get_chat_service must be synchronous for FastAPI Depends."""
        assert not inspect.iscoroutinefunction(get_chat_service), (
            "get_chat_service must be sync so FastAPI can call it without await"
        )

    def test_returns_chat_service_with_stub_factory(self) -> None:
        """get_chat_service() with a stub factory must return a ChatService instance."""
        stub = _StubLLMClientFactory()
        service = get_chat_service(factory=stub)
        assert isinstance(service, ChatService)

    def test_stub_factory_not_held_by_service(self) -> None:
        """The produced ChatService must hold the LLM client, not the factory.

        The factory is consumed in the DI layer (get_chat_service) to produce
        a client (LLMClientProtocol).  ChatService holds the *client*, not
        the factory — this verifies the boundary is correctly placed.
        """
        stub = _StubLLMClientFactory()
        service = get_chat_service(factory=stub)

        # Service must have an _llm attribute (the injected client)
        assert hasattr(service, "_llm"), "ChatService must store the injected LLM client as _llm"
        # It must NOT hold the factory
        assert not isinstance(service._llm, LLMClientFactoryProtocol), (  # type: ignore[attr-defined]
            "ChatService._llm should be an LLMClientProtocol client, not a factory"
        )

    def test_service_llm_satisfies_abstract_port(self) -> None:
        """The injected client held by ChatService must satisfy AbstractLLMPort (has invoke/stream).

        ChatService depends on AbstractLLMPort, so the injected client must at
        minimum have the ``invoke`` and ``stream`` methods that the service calls.
        """
        stub = _StubLLMClientFactory()
        service = get_chat_service(factory=stub)

        assert hasattr(service._llm, "invoke"), (  # type: ignore[attr-defined]
            "ChatService._llm must have 'invoke' method (AbstractLLMPort interface)"
        )
        assert hasattr(service._llm, "stream"), (  # type: ignore[attr-defined]
            "ChatService._llm must have 'stream' method (AbstractLLMPort interface)"
        )

    def test_different_factories_produce_independent_services(self) -> None:
        """Different factory instances must yield independent ChatService instances."""
        factory_a = _StubLLMClientFactory(response="alpha")
        factory_b = _StubLLMClientFactory(response="beta")

        service_a = get_chat_service(factory=factory_a)
        service_b = get_chat_service(factory=factory_b)

        assert service_a is not service_b

    def test_mock_factory_triggers_get_llm_client(self) -> None:
        """get_chat_service must call factory.get_llm_client() exactly once."""
        mock_client = MagicMock(spec=["ainvoke", "astream", "invoke", "stream"])
        mock_client.invoke = AsyncMock(return_value=AIMessage(content="mocked"))

        mock_factory = MagicMock(spec=LLMClientFactoryProtocol)
        mock_factory.get_llm_client.return_value = mock_client

        service = get_chat_service(factory=mock_factory)

        mock_factory.get_llm_client.assert_called_once()
        assert isinstance(service, ChatService)


# ---------------------------------------------------------------------------
# End-to-end DI flow — stub factory, no concrete imports
# ---------------------------------------------------------------------------


class TestDIWiringEndToEnd:
    """Full DI wiring: stub factory → get_chat_service → ChatService operations.

    These tests prove that the entire DI path (container → service → LLM client)
    functions correctly using *only* stub implementations — zero concrete
    infrastructure imports are touched.
    """

    @pytest.mark.asyncio
    async def test_service_complete_with_stub_factory(self) -> None:
        """complete() via DI-wired ChatService must return the stub response."""
        stub = _StubLLMClientFactory(response="DI wired response")
        service = get_chat_service(factory=stub)

        result = await service.complete([HumanMessage(content="Hello")])

        assert isinstance(result, AIMessage)
        assert result.content == "DI wired response"

    @pytest.mark.asyncio
    async def test_service_stream_with_stub_factory(self) -> None:
        """stream() via DI-wired ChatService must yield the stub chunks."""
        stub = _StubLLMClientFactory(response="streamed chunk")
        service = get_chat_service(factory=stub)

        collected: list[str] = []
        async for chunk in service.stream([HumanMessage(content="Hello")]):
            collected.append(chunk)

        assert len(collected) >= 1
        assert all(isinstance(c, str) for c in collected)
        assert collected[0] == "streamed chunk"

    @pytest.mark.asyncio
    async def test_service_complete_with_mock_factory(self) -> None:
        """complete() via mock factory must call the mock's invoke (AbstractLLMPort)."""
        expected = AIMessage(content="mock response")
        mock_client = MagicMock(spec=["ainvoke", "astream", "invoke", "stream"])
        mock_client.invoke = AsyncMock(return_value=expected)

        mock_factory = MagicMock(spec=LLMClientFactoryProtocol)
        mock_factory.get_llm_client.return_value = mock_client

        service = get_chat_service(factory=mock_factory)
        result = await service.complete([HumanMessage(content="test")])

        assert result is expected
        mock_client.invoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_complete_with_multi_turn_messages(self) -> None:
        """DI-wired service must handle multi-turn conversation messages."""
        from langchain_core.messages import SystemMessage

        stub = _StubLLMClientFactory(response="multi-turn ok")
        service = get_chat_service(factory=stub)

        messages = [
            SystemMessage(content="You are helpful."),
            HumanMessage(content="What is 2+2?"),
        ]
        result = await service.complete(messages)

        assert isinstance(result, AIMessage)
        assert result.content == "multi-turn ok"


# ---------------------------------------------------------------------------
# Provider isolation — no direct concrete references anywhere in DI path
# ---------------------------------------------------------------------------


class TestProviderIsolationInDILayer:
    """The entire DI path must be free of direct provider/concrete references."""

    def test_chat_service_module_has_no_concrete_provider_imports(self) -> None:
        """Regression: service.py must not import concrete provider classes.

        This verifies the domain isolation guarantee is maintained even after
        introducing the container layer.
        """
        import domains.chat.service as service_module

        forbidden = ["ChatLiteLLM", "LLMClient", "DefaultLLMClientFactory", "ProviderFactory"]
        module_vars = vars(service_module)
        for name in forbidden:
            assert name not in module_vars, (
                f"service.py imported concrete class {name!r} — "
                "the domain service must depend only on ports (interfaces), "
                "not on infrastructure classes"
            )

    def test_chat_service_constructor_typed_as_abstract_port(self) -> None:
        """ChatService.__init__ llm_client parameter must be typed as AbstractLLMPort.

        This is the *service-level* interface boundary: the service constructor
        accepts the domain's abstract port, not a concrete factory or protocol class.
        Using AbstractLLMPort ensures zero runtime imports of LangChain/litellm.
        """
        hints = typing.get_type_hints(
            ChatService.__init__,
            localns={"AbstractLLMPort": AbstractLLMPort},
        )
        resolved = hints.get("llm_client")

        assert resolved is AbstractLLMPort, (
            f"ChatService.__init__ 'llm_client' must be AbstractLLMPort, got {resolved!r}"
        )

    def test_full_di_path_requires_zero_concrete_imports(self) -> None:
        """The entire path container→service→client must work with zero concrete imports.

        This is the definitive isolation test: using only stub implementations
        (no ChatLiteLLM, no DefaultLLMClientFactory, no langchain_litellm),
        the DI wiring must produce a functional ChatService.
        """
        # Both factory and client are stubs — no concrete provider class touched
        factory: LLMClientFactoryProtocol = _StubLLMClientFactory()
        service = get_chat_service(factory=factory)

        assert isinstance(service, ChatService)
        # Service holds a client that satisfies the AbstractLLMPort contract
        assert hasattr(service._llm, "invoke"), (  # type: ignore[attr-defined]
            "ChatService._llm must have invoke (AbstractLLMPort) after zero-concrete DI"
        )
        assert hasattr(service._llm, "stream"), (  # type: ignore[attr-defined]
            "ChatService._llm must have stream (AbstractLLMPort) after zero-concrete DI"
        )

    def test_container_module_imports_only_interface_from_ports(self) -> None:
        """The container imports LLMClientFactoryProtocol from ports (interface module)."""
        import domains.chat.container as container_module

        module_vars = vars(container_module)

        # The interface type IS expected in the namespace (it's the binding target)
        assert "LLMClientFactoryProtocol" in module_vars, (
            "LLMClientFactoryProtocol must be importable from the container module "
            "so callers can use it for type annotations"
        )

    def test_get_llm_factory_binding_produces_interface_compatible_object(self) -> None:
        """The interface-bound factory must produce an LLMClientProtocol-compatible client.

        Verifies the entire chain: container factory → client → protocol compatibility.
        """
        with (
            patch("domains.chat.llm_client.get_settings") as mock_settings,
            patch("infra.llm.provider_factory.ChatLiteLLM"),
        ):
            mock_settings.return_value.llm.provider.value = "openai"
            mock_settings.return_value.llm.as_litellm_kwargs.return_value = {
                "model": "openai/gpt-4o-mini",
                "api_key": "sk-test",
            }
            mock_settings.return_value.llm.default_model = "gpt-4o-mini"
            factory = get_llm_factory()
            client = factory.get_llm_client()

        assert isinstance(client, LLMClientProtocol), (
            "Client produced by the registered factory must satisfy LLMClientProtocol"
        )
