"""Chat domain DI container — binds concrete factories to interface types.

Implements the Dependency Injection layer for the chat domain.  The container:

* Registers :class:`~app.domains.chat.llm_client.DefaultLLMClientFactory`
  as :class:`LLMClientFactoryProtocol` (concrete bound to interface).
* Exposes FastAPI-compatible dependency functions typed **only** against the
  abstract interface so all other callers never reference concrete infra classes.

Architecture boundary
---------------------
This module is the **single registration point** that is allowed to bridge the
infrastructure layer (:mod:`llm_client`) with the domain interface layer
(:mod:`ports`).  All other chat-domain code and application code must depend
on the interface alone, not on the concrete factory.

Binding pattern::

    # ── What happens under the hood ────────────────────────────────────────
    #
    # LLMClientFactoryProtocol  ←→  DefaultLLMClientFactory   (registered here)
    #        ↑                              ↑
    #   interface type             concrete implementation
    #
    # FastAPI Depends graph:
    #   get_llm_factory()  →  LLMClientFactoryProtocol
    #          ↓
    #   get_chat_service(factory: LLMClientFactoryProtocol)  →  ChatService
    #          ↓ (factory.get_llm_client())
    #   ChatService(llm_client=<AbstractLLMPort>)

Usage in a FastAPI router::

    from fastapi import APIRouter, Depends
    from domains.chat.container import get_chat_service
    from domains.chat.service import ChatService

    router = APIRouter()

    @router.post("/complete")
    async def chat_complete(
        service: ChatService = Depends(get_chat_service),
    ) -> dict:
        ...

Testing — inject a stub factory via dependency override::

    from unittest.mock import AsyncMock, MagicMock
    from langchain_core.messages.ai import AIMessage

    from domains.chat.container import get_llm_factory
    from domains.chat.ports import LLMClientFactoryProtocol, LLMClientProtocol

    class StubFactory:
        def get_llm_client(self) -> LLMClientProtocol:
            mock = MagicMock(spec=["ainvoke", "astream"])
            mock.ainvoke = AsyncMock(return_value=AIMessage(content="stub"))
            return mock

    # Override at the factory level — get_chat_service picks it up automatically
    app.dependency_overrides[get_llm_factory] = lambda: StubFactory()
"""

from __future__ import annotations

from fastapi import Depends

from domains.chat.ports import LLMClientFactoryProtocol
from domains.chat.service import ChatService

# ---------------------------------------------------------------------------
# Factory binding — concrete registered as interface
# ---------------------------------------------------------------------------


def get_llm_factory() -> LLMClientFactoryProtocol:
    """Return the registered :class:`LLMClientFactoryProtocol` implementation.

    Binds :class:`~app.domains.chat.llm_client.DefaultLLMClientFactory`
    to the :class:`LLMClientFactoryProtocol` interface at runtime.  Callers
    always receive an object typed as the interface — they never need to import
    the concrete factory class.

    The concrete import lives **inside** this function (not at module level) so
    that the ``container`` module's namespace stays clean of infrastructure
    references.  Tests can verify this by inspecting ``vars(container_module)``.

    To swap the LLM factory (e.g. in tests or a different provider adapter),
    override this dependency in the FastAPI application::

        app.dependency_overrides[get_llm_factory] = lambda: MyCustomFactory()

    Returns
    -------
    LLMClientFactoryProtocol
        A concrete factory satisfying :class:`LLMClientFactoryProtocol`.
    """
    # ── Lazy import keeps DefaultLLMClientFactory out of module namespace ─────
    # Any code that imports `from container import *` or inspects
    # `vars(container)` will NOT see DefaultLLMClientFactory, enforcing the
    # architecture boundary at inspection time as well as import time.
    from domains.chat.llm_client import DefaultLLMClientFactory

    return DefaultLLMClientFactory()


# ---------------------------------------------------------------------------
# Service builder — wires factory (interface) → ChatService
# ---------------------------------------------------------------------------


def get_chat_service(
    factory: LLMClientFactoryProtocol = Depends(get_llm_factory),
) -> ChatService:
    """Build a :class:`ChatService` using an injected :class:`LLMClientFactoryProtocol`.

    This is the canonical FastAPI dependency for obtaining a configured
    :class:`ChatService`.  The ``factory`` parameter is typed against the
    *interface* (``LLMClientFactoryProtocol``) rather than any concrete class,
    enforcing the DI boundary:

    * **Production** — FastAPI resolves ``factory`` via :func:`get_llm_factory`,
      which returns a :class:`DefaultLLMClientFactory`.
    * **Tests** — override :func:`get_llm_factory` with a stub factory;
      ``get_chat_service`` transparently wires the service with the stub client.

    The service constructor (:class:`ChatService`) accepts
    :class:`~app.domains.chat.ports.LLMClientProtocol`, not the factory.
    The factory is consumed here (in the DI layer), keeping ``ChatService``
    oblivious to factory details.

    Parameters
    ----------
    factory:
        Any object satisfying :class:`LLMClientFactoryProtocol`.  FastAPI
        resolves this via :func:`get_llm_factory` unless overridden via
        ``app.dependency_overrides``.

    Returns
    -------
    ChatService
        A domain service configured with the LLM client produced by *factory*.

    Examples
    --------
    In a router (FastAPI resolves deps automatically)::

        @router.post("/messages")
        async def create_message(
            body: ChatRequest,
            service: ChatService = Depends(get_chat_service),
        ) -> dict:
            result = await service.complete(messages=body.to_langchain_messages())
            return {"content": result.content}

    In tests (call directly with a stub factory)::

        stub = StubFactory()
        service = get_chat_service(factory=stub)
        result = await service.complete([HumanMessage(content="hi")])
    """
    llm_client = factory.get_llm_client()
    return ChatService(llm_client=llm_client)
