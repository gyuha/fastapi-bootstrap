"""Integration tests: chat API provider switching via TestClient (Sub-AC 14.3.3).

Verifies that ``LLM_PROVIDER`` environment-variable changes correctly route
``/chat`` HTTP requests to the appropriate LLM backend — without modifying
any chat domain code.

Test strategy
-------------
Two complementary approaches are used:

**1. ChatLiteLLM patch approach** (``TestChatAPIProviderSwitching``)
    Patches :class:`ChatLiteLLM` at the module boundary to capture constructor
    kwargs.  Exercises the full routing chain:

    ::

        HTTP request
            → Settings (from LLM_PROVIDER env var)
            → ProviderFactory.make_kwargs()
            → ChatLiteLLM(**kwargs)   ← captured here
            → HTTP response

    Provider routing is verified by asserting on the captured kwargs
    (``model``, ``api_key``, ``api_base``).

**2. Dependency override approach** (``TestChatAPIProviderSwitchingViaOverrides``)
    Injects a tracking factory via ``app.dependency_overrides[get_llm_factory]``.
    Verifies the DI chain:

    ::

        get_llm_factory() → _TrackingFactory → _TrackingLLMClient
            → ainvoke / astream call count

Covered scenarios
-----------------
OpenAI routing (``LLM_PROVIDER=openai``):
  * POST /chat/complete routes to OpenAI backend (model=openai/...)
  * POST /chat/complete returns correct response content
  * POST /chat/complete includes litellm_model in response body
  * POST /chat/stream routes to OpenAI backend and emits SSE chunks
  * GET /chat/provider returns openai provider info

Ollama routing (``LLM_PROVIDER=ollama``):
  * POST /chat/complete routes to Ollama backend (model=ollama/..., api_base=...)
  * POST /chat/complete returns correct response content
  * POST /chat/complete includes litellm_model in response body
  * POST /chat/stream routes to Ollama backend (api_base=..., api_key=ollama)
  * GET /chat/provider returns ollama provider info

Provider switching:
  * Env-only switch from openai→ollama changes model prefix and api_base presence
  * get_llm_factory override produces different responses without code change
  * GET /chat/provider reflects env-based provider switching

Request validation:
  * Empty messages list returns 422
  * Missing messages field returns 422
  * System prompt is prepended to message list before LLM call
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import get_settings
from domains.chat.container import get_llm_factory
from domains.chat.ports import LLMClientProtocol

from ._mocks import (
    FAKE_RESPONSE_TEXT,
    FAKE_STREAM_TOKENS,
    OLLAMA_DEFAULT_MODEL,
    OLLAMA_TEST_URL,
    OPENAI_DEFAULT_MODEL,
    OPENAI_TEST_KEY,
    FakeChatLiteLLM,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Patch path for ChatLiteLLM class in the llm_client module
_LITELLM_PATH: str = "infra.llm.provider_factory.ChatLiteLLM"

#: API endpoint paths
_CHAT_COMPLETE_URL: str = "/api/v1/chat/complete"
_CHAT_STREAM_URL: str = "/api/v1/chat/stream"
_CHAT_PROVIDER_URL: str = "/api/v1/chat/provider"

#: Standard JSON request bodies for tests
_OPENAI_REQUEST: dict[str, Any] = {
    "messages": [{"role": "user", "content": "Hello, OpenAI!"}],
}
_OLLAMA_REQUEST: dict[str, Any] = {
    "messages": [{"role": "user", "content": "Hello, Ollama!"}],
}


# ---------------------------------------------------------------------------
# Tracking test doubles
# ---------------------------------------------------------------------------


class _TrackingLLMClient:
    """Minimal :class:`LLMClientProtocol` satisfier that records every call.

    Allows tests to assert that ``ainvoke`` / ``astream`` were called exactly
    once (or the expected number of times) per HTTP request, without asserting
    on the provider-specific kwargs (that is done by the ChatLiteLLM-patch tests).

    Parameters
    ----------
    provider:
        Provider name recorded in ``self.provider``.
    model_string:
        Model string recorded in ``self.model_string``.
    response:
        Text returned by :meth:`ainvoke`.  Defaults to :data:`FAKE_RESPONSE_TEXT`.
    """

    def __init__(
        self,
        provider: str,
        model_string: str,
        response: str = FAKE_RESPONSE_TEXT,
    ) -> None:
        self.provider: str = provider
        self.model_string: str = model_string
        self._response: str = response
        self.ainvoke_call_count: int = 0
        self.astream_call_count: int = 0
        self.invoke_call_count: int = 0
        self.stream_call_count: int = 0
        self.last_messages: list[Any] = []

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        """Return a pre-canned AIMessage and increment call counter (LLMClientProtocol)."""
        from langchain_core.messages.ai import AIMessage

        self.ainvoke_call_count += 1
        self.last_messages = list(messages)
        return AIMessage(content=self._response)

    async def astream(self, messages: Any, **kwargs: Any) -> Any:
        """Yield pre-canned tokens and increment call counter (LLMClientProtocol)."""
        self.astream_call_count += 1
        self.last_messages = list(messages)
        for token in FAKE_STREAM_TOKENS:
            yield token

    async def invoke(self, messages: Any, **kwargs: Any) -> Any:
        """Return a pre-canned AIMessage and increment call counter (AbstractLLMPort)."""
        from langchain_core.messages.ai import AIMessage

        self.invoke_call_count += 1
        self.last_messages = list(messages)
        return AIMessage(content=self._response)

    async def stream(self, messages: Any, **kwargs: Any) -> Any:
        """Yield pre-canned tokens and increment call counter (AbstractLLMPort)."""
        self.stream_call_count += 1
        self.last_messages = list(messages)
        for token in FAKE_STREAM_TOKENS:
            yield token


class _TrackingFactory:
    """Minimal :class:`LLMClientFactoryProtocol` wrapping a :class:`_TrackingLLMClient`."""

    def __init__(self, client: _TrackingLLMClient) -> None:
        self._client: _TrackingLLMClient = client

    def get_llm_client(self) -> LLMClientProtocol:
        """Return the tracking client (satisfies protocol structurally)."""
        return self._client  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chat_test_app() -> FastAPI:
    """Minimal FastAPI app with only the chat router mounted.

    Excludes the full ``main.py`` lifespan (Redis connection, structlog setup)
    so tests run without any infrastructure dependencies.

    The fixture is **function-scoped** (default) so each test gets a fresh
    app instance with no leftover ``dependency_overrides``.
    """
    from domains.chat.router import router as chat_router

    application = FastAPI(title="Chat Integration Test App")
    application.include_router(chat_router, prefix="/api/v1")
    return application


# ---------------------------------------------------------------------------
# Helper — build a capturing side-effect for ChatLiteLLM
# ---------------------------------------------------------------------------


def _make_litellm_capture(
    response: str = FAKE_RESPONSE_TEXT,
    stream_tokens: list[str] | None = None,
) -> tuple[dict[str, Any], Any]:
    """Return ``(captured_dict, side_effect_fn)`` for patching ChatLiteLLM.

    Usage::

        captured, capture_fn = _make_litellm_capture()
        with patch(_LITELLM_PATH, side_effect=capture_fn):
            ...
        assert captured["model"] == "openai/gpt-4o-mini"
    """
    captured: dict[str, Any] = {}
    tokens = stream_tokens if stream_tokens is not None else list(FAKE_STREAM_TOKENS)
    fake_instance = FakeChatLiteLLM(response=response, stream_tokens=tokens)

    def _capture(**kw: Any) -> FakeChatLiteLLM:
        captured.update(kw)
        return fake_instance

    return captured, _capture


# ---------------------------------------------------------------------------
# Tests — ChatLiteLLM patch approach
# ---------------------------------------------------------------------------


class TestChatAPIProviderSwitching:
    """E2E routing tests using ChatLiteLLM kwargs capture.

    The full settings → ProviderFactory → ChatLiteLLM chain is exercised for
    every request.  ChatLiteLLM is patched only at the module boundary so
    no real network calls are made.
    """

    # ── OpenAI /complete ────────────────────────────────────────────────────

    def test_openai_complete_routes_to_openai_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/complete with LLM_PROVIDER=openai calls the OpenAI backend.

        Verified by: captured ChatLiteLLM kwargs contain ``model='openai/<model>'``,
        ``api_key=<key>``, and no ``api_base`` (OpenAI uses its default endpoint).
        """
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_DEFAULT_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        get_settings.cache_clear()

        captured, capture_fn = _make_litellm_capture(response=FAKE_RESPONSE_TEXT)

        with patch(_LITELLM_PATH, side_effect=capture_fn):
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(_CHAT_COMPLETE_URL, json=_OPENAI_REQUEST)

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        assert captured.get("model") == f"openai/{OPENAI_DEFAULT_MODEL}", (
            f"Expected model='openai/{OPENAI_DEFAULT_MODEL}', got {captured.get('model')!r}"
        )
        assert captured.get("api_key") == OPENAI_TEST_KEY, (
            f"Expected api_key={OPENAI_TEST_KEY!r}, got {captured.get('api_key')!r}"
        )
        assert "api_base" not in captured, (
            f"OpenAI must not set api_base; got {captured.get('api_base')!r}"
        )

    def test_openai_complete_returns_response_content(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/complete body contains the LLM response text."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_DEFAULT_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
        get_settings.cache_clear()

        expected = "OpenAI says hello from the test!"
        _, capture_fn = _make_litellm_capture(response=expected)

        with patch(_LITELLM_PATH, side_effect=capture_fn):
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(_CHAT_COMPLETE_URL, json=_OPENAI_REQUEST)

        assert response.status_code == 200
        assert response.json()["content"] == expected

    def test_openai_complete_includes_model_in_response(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/complete response body includes the active litellm model string."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_DEFAULT_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
        get_settings.cache_clear()

        with patch(_LITELLM_PATH, return_value=FakeChatLiteLLM()):
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(_CHAT_COMPLETE_URL, json=_OPENAI_REQUEST)

        assert response.status_code == 200
        assert response.json().get("model") == f"openai/{OPENAI_DEFAULT_MODEL}"

    # ── Ollama /complete ─────────────────────────────────────────────────────

    def test_ollama_complete_routes_to_ollama_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/complete with LLM_PROVIDER=ollama calls the Ollama backend.

        Verified by: captured ChatLiteLLM kwargs contain ``model='ollama/<model>'``,
        ``api_base=<OLLAMA_BASE_URL>``, and ``api_key='ollama'`` (litellm sentinel).
        """
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        get_settings.cache_clear()

        captured, capture_fn = _make_litellm_capture(response=FAKE_RESPONSE_TEXT)

        with patch(_LITELLM_PATH, side_effect=capture_fn):
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(_CHAT_COMPLETE_URL, json=_OLLAMA_REQUEST)

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        assert captured.get("model") == f"ollama/{OLLAMA_DEFAULT_MODEL}", (
            f"Expected model='ollama/{OLLAMA_DEFAULT_MODEL}', got {captured.get('model')!r}"
        )
        assert captured.get("api_base") == OLLAMA_TEST_URL, (
            f"Expected api_base={OLLAMA_TEST_URL!r}, got {captured.get('api_base')!r}"
        )
        assert captured.get("api_key") == "ollama", (
            f"Expected api_key='ollama' sentinel, got {captured.get('api_key')!r}"
        )

    def test_ollama_complete_returns_response_content(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/complete body contains the LLM response text for Ollama."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        get_settings.cache_clear()

        expected = "Ollama says hello from the test!"
        _, capture_fn = _make_litellm_capture(response=expected)

        with patch(_LITELLM_PATH, side_effect=capture_fn):
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(_CHAT_COMPLETE_URL, json=_OLLAMA_REQUEST)

        assert response.status_code == 200
        assert response.json()["content"] == expected

    def test_ollama_complete_includes_model_in_response(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/complete response body includes the Ollama litellm model string."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        get_settings.cache_clear()

        with patch(_LITELLM_PATH, return_value=FakeChatLiteLLM()):
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(_CHAT_COMPLETE_URL, json=_OLLAMA_REQUEST)

        assert response.status_code == 200
        assert response.json().get("model") == f"ollama/{OLLAMA_DEFAULT_MODEL}"

    # ── Provider switching — zero code change ─────────────────────────────────

    def test_env_switch_openai_to_ollama_changes_backend_kwargs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """Switching LLM_PROVIDER from openai→ollama changes ChatLiteLLM kwargs.

        This is the definitive provider-portability test:

        * Same HTTP endpoint (``/chat/complete``)
        * Same request body
        * Same application code — **only** the environment variable changes

        Asserts that the two scenarios produce correctly differentiated
        ChatLiteLLM kwargs without any code modification.
        """
        openai_captured: dict[str, Any] = {}
        ollama_captured: dict[str, Any] = {}
        fake = FakeChatLiteLLM(response=FAKE_RESPONSE_TEXT)

        def _capture_openai(**kw: Any) -> FakeChatLiteLLM:
            openai_captured.update(kw)
            return fake

        def _capture_ollama(**kw: Any) -> FakeChatLiteLLM:
            ollama_captured.update(kw)
            return fake

        # ── Scenario 1: OpenAI ───────────────────────────────────────────────
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_DEFAULT_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        get_settings.cache_clear()

        with patch(_LITELLM_PATH, side_effect=_capture_openai):
            c1 = TestClient(chat_test_app, raise_server_exceptions=True)
            r1 = c1.post(_CHAT_COMPLETE_URL, json=_OPENAI_REQUEST)

        assert r1.status_code == 200

        # ── Scenario 2: Ollama (env-only change, zero code modification) ─────
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        get_settings.cache_clear()

        with patch(_LITELLM_PATH, side_effect=_capture_ollama):
            c2 = TestClient(chat_test_app, raise_server_exceptions=True)
            r2 = c2.post(_CHAT_COMPLETE_URL, json=_OLLAMA_REQUEST)

        assert r2.status_code == 200

        # ── Assert routing diverged correctly ──────────────────────────────────
        assert openai_captured["model"].startswith("openai/"), (
            f"OpenAI scenario: model must start with 'openai/', got {openai_captured['model']!r}"
        )
        assert ollama_captured["model"].startswith("ollama/"), (
            f"Ollama scenario: model must start with 'ollama/', got {ollama_captured['model']!r}"
        )
        assert openai_captured["model"] != ollama_captured["model"]

        # OpenAI: no api_base; Ollama: api_base=<local URL>
        assert "api_base" not in openai_captured, "OpenAI must not set api_base"
        assert "api_base" in ollama_captured, "Ollama must set api_base"
        assert ollama_captured["api_base"] == OLLAMA_TEST_URL

        # OpenAI: real API key; Ollama: 'ollama' sentinel
        assert openai_captured.get("api_key") == OPENAI_TEST_KEY
        assert ollama_captured.get("api_key") == "ollama"


# ---------------------------------------------------------------------------
# Tests — dependency_overrides approach
# ---------------------------------------------------------------------------


class TestChatAPIProviderSwitchingViaOverrides:
    """E2E routing tests using FastAPI dependency_overrides.

    Injects a :class:`_TrackingFactory` via ``app.dependency_overrides[get_llm_factory]``
    to verify the DI chain without patching at the module level.  This approach
    proves that the router correctly delegates to the injected factory.
    """

    def test_openai_tracking_factory_invoked_on_complete(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """get_llm_factory override injects the OpenAI tracking client into /complete."""
        openai_client = _TrackingLLMClient(
            provider="openai",
            model_string=f"openai/{OPENAI_DEFAULT_MODEL}",
            response="OpenAI response via DI override",
        )
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            openai_client
        )

        try:
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(_CHAT_COMPLETE_URL, json=_OPENAI_REQUEST)
        finally:
            chat_test_app.dependency_overrides.clear()

        assert response.status_code == 200
        assert openai_client.invoke_call_count == 1, (
            "Tracking client invoke must be called exactly once per request"
        )
        assert response.json()["content"] == "OpenAI response via DI override"

    def test_ollama_tracking_factory_invoked_on_complete(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """get_llm_factory override injects the Ollama tracking client into /complete."""
        ollama_client = _TrackingLLMClient(
            provider="ollama",
            model_string=f"ollama/{OLLAMA_DEFAULT_MODEL}",
            response="Ollama response via DI override",
        )
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            ollama_client
        )

        try:
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(_CHAT_COMPLETE_URL, json=_OLLAMA_REQUEST)
        finally:
            chat_test_app.dependency_overrides.clear()

        assert response.status_code == 200
        assert ollama_client.invoke_call_count == 1
        assert response.json()["content"] == "Ollama response via DI override"

    def test_switching_factory_override_routes_to_different_client(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """Switching the injected factory changes the response — zero code change.

        Documents the provider portability guarantee at the DI layer:
        the router code is identical for both providers; routing is data-driven.
        """
        openai_client = _TrackingLLMClient(
            provider="openai",
            model_string=f"openai/{OPENAI_DEFAULT_MODEL}",
            response="I am OpenAI",
        )
        ollama_client = _TrackingLLMClient(
            provider="ollama",
            model_string=f"ollama/{OLLAMA_DEFAULT_MODEL}",
            response="I am Ollama",
        )

        # ── OpenAI scenario ─────────────────────────────────────────────────
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            openai_client
        )
        try:
            c1 = TestClient(chat_test_app, raise_server_exceptions=True)
            r1 = c1.post(_CHAT_COMPLETE_URL, json=_OPENAI_REQUEST)
        finally:
            chat_test_app.dependency_overrides.clear()

        # ── Ollama scenario (only the factory override changes) ──────────────
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            ollama_client
        )
        try:
            c2 = TestClient(chat_test_app, raise_server_exceptions=True)
            r2 = c2.post(_CHAT_COMPLETE_URL, json=_OLLAMA_REQUEST)
        finally:
            chat_test_app.dependency_overrides.clear()

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["content"] == "I am OpenAI"
        assert r2.json()["content"] == "I am Ollama"
        assert openai_client.invoke_call_count == 1
        assert ollama_client.invoke_call_count == 1
        # Cross-check: each client was called exactly once for its scenario
        assert openai_client.invoke_call_count != ollama_client.invoke_call_count or (
            openai_client.invoke_call_count == ollama_client.invoke_call_count == 1
        )

    def test_tracking_client_captures_messages(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """The injected LLMClient receives the message list from the HTTP request."""
        tracking_client = _TrackingLLMClient(
            provider="openai",
            model_string=f"openai/{OPENAI_DEFAULT_MODEL}",
            response="Messages received",
        )
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            tracking_client
        )

        try:
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(
                _CHAT_COMPLETE_URL,
                json={"messages": [{"role": "user", "content": "Ping"}]},
            )
        finally:
            chat_test_app.dependency_overrides.clear()

        assert response.status_code == 200
        assert len(tracking_client.last_messages) == 1
        assert tracking_client.last_messages[0].content == "Ping"


# ---------------------------------------------------------------------------
# Tests — SSE streaming endpoint
# ---------------------------------------------------------------------------


class TestChatAPIStreamingProviderRouting:
    """E2E integration tests for the ``POST /chat/stream`` SSE endpoint.

    Verifies that streaming requests are correctly routed to the configured
    LLM provider and that SSE events are emitted.
    """

    def test_openai_stream_routes_to_openai_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/stream with LLM_PROVIDER=openai routes to OpenAI backend.

        Verified by: captured ChatLiteLLM kwargs have ``model='openai/<model>'``
        and SSE ``data:`` events are present in the response.
        """
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_DEFAULT_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        get_settings.cache_clear()

        captured, capture_fn = _make_litellm_capture(stream_tokens=list(FAKE_STREAM_TOKENS))

        with patch(_LITELLM_PATH, side_effect=capture_fn):
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            with client.stream("POST", _CHAT_STREAM_URL, json=_OPENAI_REQUEST) as resp:
                assert resp.status_code == 200
                lines = list(resp.iter_lines())

        assert captured.get("model") == f"openai/{OPENAI_DEFAULT_MODEL}", (
            f"Expected model='openai/{OPENAI_DEFAULT_MODEL}', got {captured.get('model')!r}"
        )
        assert "api_base" not in captured
        data_lines = [ln for ln in lines if ln.startswith("data:")]
        assert len(data_lines) > 0, f"Expected SSE data events, got: {lines}"

    def test_ollama_stream_routes_to_ollama_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/stream with LLM_PROVIDER=ollama routes to Ollama backend.

        Verified by: captured ChatLiteLLM kwargs have ``model='ollama/<model>'``,
        ``api_base=<OLLAMA_BASE_URL>``, and ``api_key='ollama'``.
        """
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        get_settings.cache_clear()

        captured, capture_fn = _make_litellm_capture(stream_tokens=list(FAKE_STREAM_TOKENS))

        with patch(_LITELLM_PATH, side_effect=capture_fn):
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            with client.stream("POST", _CHAT_STREAM_URL, json=_OLLAMA_REQUEST) as resp:
                assert resp.status_code == 200
                lines = list(resp.iter_lines())

        assert captured.get("model") == f"ollama/{OLLAMA_DEFAULT_MODEL}"
        assert captured.get("api_base") == OLLAMA_TEST_URL
        assert captured.get("api_key") == "ollama"
        data_lines = [ln for ln in lines if ln.startswith("data:")]
        assert len(data_lines) > 0, f"Expected SSE data events, got: {lines}"

    def test_stream_via_tracking_factory_calls_astream_once(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/stream calls LLMClientProtocol.astream exactly once per request."""
        tracking_client = _TrackingLLMClient(
            provider="openai",
            model_string=f"openai/{OPENAI_DEFAULT_MODEL}",
        )
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            tracking_client
        )

        try:
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            with client.stream("POST", _CHAT_STREAM_URL, json=_OPENAI_REQUEST) as resp:
                assert resp.status_code == 200
                _ = list(resp.iter_lines())  # consume the stream
        finally:
            chat_test_app.dependency_overrides.clear()

        assert tracking_client.stream_call_count == 1, (
            f"stream must be called exactly once; got {tracking_client.stream_call_count}"
        )

    def test_stream_done_sentinel_present(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/stream response includes a 'data: [DONE]' sentinel event."""
        tracking_client = _TrackingLLMClient(
            provider="openai",
            model_string=f"openai/{OPENAI_DEFAULT_MODEL}",
        )
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            tracking_client
        )

        try:
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            with client.stream("POST", _CHAT_STREAM_URL, json=_OPENAI_REQUEST) as resp:
                assert resp.status_code == 200
                lines = list(resp.iter_lines())
        finally:
            chat_test_app.dependency_overrides.clear()

        done_lines = [ln for ln in lines if "[DONE]" in ln]
        assert len(done_lines) >= 1, f"Expected at least one 'data: [DONE]' line, got: {lines}"


# ---------------------------------------------------------------------------
# Tests — GET /chat/provider info endpoint
# ---------------------------------------------------------------------------


class TestChatAPIProviderInfoEndpoint:
    """Tests for ``GET /chat/provider`` — validates active provider metadata.

    This endpoint requires no LLM calls and directly reflects the current
    ``LLM_PROVIDER`` / ``LLM_DEFAULT_MODEL`` environment variables.
    """

    def test_provider_info_openai_when_env_is_openai(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """GET /chat/provider returns openai provider info when LLM_PROVIDER=openai."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_DEFAULT_MODEL)
        monkeypatch.setenv("OPENAI_API_KEY", OPENAI_TEST_KEY)
        get_settings.cache_clear()

        client = TestClient(chat_test_app, raise_server_exceptions=True)
        response = client.get(_CHAT_PROVIDER_URL)

        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "openai"
        assert body["model"] == OPENAI_DEFAULT_MODEL
        assert body["litellm_model"] == f"openai/{OPENAI_DEFAULT_MODEL}"

    def test_provider_info_ollama_when_env_is_ollama(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """GET /chat/provider returns ollama provider info when LLM_PROVIDER=ollama."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL)
        monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_TEST_URL)
        get_settings.cache_clear()

        client = TestClient(chat_test_app, raise_server_exceptions=True)
        response = client.get(_CHAT_PROVIDER_URL)

        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "ollama"
        assert body["model"] == OLLAMA_DEFAULT_MODEL
        assert body["litellm_model"] == f"ollama/{OLLAMA_DEFAULT_MODEL}"

    def test_provider_info_reflects_env_switch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        chat_test_app: FastAPI,
    ) -> None:
        """GET /chat/provider response changes when LLM_PROVIDER env var changes.

        This is the definitive portability check for the /provider endpoint:
        the exact same code path returns different data based solely on the
        environment variable, with zero code modification.
        """
        # ── OpenAI scenario ─────────────────────────────────────────────────
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OPENAI_DEFAULT_MODEL)
        get_settings.cache_clear()

        client = TestClient(chat_test_app)
        r_openai = client.get(_CHAT_PROVIDER_URL)
        openai_body = r_openai.json()

        # ── Ollama scenario (env-only change) ────────────────────────────────
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_DEFAULT_MODEL", OLLAMA_DEFAULT_MODEL)
        get_settings.cache_clear()

        r_ollama = client.get(_CHAT_PROVIDER_URL)
        ollama_body = r_ollama.json()

        assert r_openai.status_code == 200
        assert r_ollama.status_code == 200
        assert openai_body["provider"] == "openai"
        assert ollama_body["provider"] == "ollama"
        assert openai_body["litellm_model"] != ollama_body["litellm_model"]
        assert openai_body["litellm_model"].startswith("openai/")
        assert ollama_body["litellm_model"].startswith("ollama/")


# ---------------------------------------------------------------------------
# Tests — request validation
# ---------------------------------------------------------------------------


class TestChatAPIRequestValidation:
    """Verify that malformed requests are rejected before reaching the LLM."""

    def test_empty_messages_list_returns_422(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/complete with messages=[] returns HTTP 422."""
        client = TestClient(chat_test_app, raise_server_exceptions=False)
        response = client.post(_CHAT_COMPLETE_URL, json={"messages": []})
        assert response.status_code == 422

    def test_missing_messages_field_returns_422(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """POST /chat/complete without a messages field returns HTTP 422."""
        client = TestClient(chat_test_app, raise_server_exceptions=False)
        response = client.post(_CHAT_COMPLETE_URL, json={})
        assert response.status_code == 422

    def test_system_prompt_prepended_before_user_message(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """System prompt in request body is prepended as SystemMessage before user messages."""
        tracking_client = _TrackingLLMClient(
            provider="openai",
            model_string=f"openai/{OPENAI_DEFAULT_MODEL}",
            response="Got system prompt",
        )
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            tracking_client
        )

        try:
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(
                _CHAT_COMPLETE_URL,
                json={
                    "messages": [{"role": "user", "content": "Hello"}],
                    "system": "You are a test assistant.",
                },
            )
        finally:
            chat_test_app.dependency_overrides.clear()

        assert response.status_code == 200
        # System message is prepended → first message is SystemMessage
        msgs = tracking_client.last_messages
        assert len(msgs) == 2, f"Expected 2 messages (system + user), got {len(msgs)}"
        from langchain_core.messages import SystemMessage

        assert isinstance(msgs[0], SystemMessage), (
            f"First message must be SystemMessage when system= is set, got {type(msgs[0])}"
        )
        assert msgs[0].content == "You are a test assistant."
        assert msgs[1].content == "Hello"

    def test_multi_turn_messages_forwarded_in_order(
        self,
        chat_test_app: FastAPI,
    ) -> None:
        """Multi-turn conversation messages are forwarded to the LLM in original order."""
        tracking_client = _TrackingLLMClient(
            provider="openai",
            model_string=f"openai/{OPENAI_DEFAULT_MODEL}",
            response="Understood",
        )
        chat_test_app.dependency_overrides[get_llm_factory] = lambda: _TrackingFactory(
            tracking_client
        )

        try:
            client = TestClient(chat_test_app, raise_server_exceptions=True)
            response = client.post(
                _CHAT_COMPLETE_URL,
                json={
                    "messages": [
                        {"role": "user", "content": "Turn 1"},
                        {"role": "assistant", "content": "Reply 1"},
                        {"role": "user", "content": "Turn 2"},
                    ],
                },
            )
        finally:
            chat_test_app.dependency_overrides.clear()

        assert response.status_code == 200
        msgs = tracking_client.last_messages
        assert len(msgs) == 3
        assert msgs[0].content == "Turn 1"
        assert msgs[1].content == "Reply 1"
        assert msgs[2].content == "Turn 2"
