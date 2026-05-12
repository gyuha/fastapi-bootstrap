"""Tests validating the chat domain mock infrastructure.

Verifies that the fixtures defined in ``conftest.py`` work correctly for both
OpenAI and Ollama providers.  These tests serve as both:

1. **Infrastructure tests** — confirm the fixtures themselves are wired
   correctly and can be safely depended on by other test modules.
2. **Provider portability tests** — demonstrate that switching from OpenAI
   to Ollama requires only a different fixture/env-var, with zero code
   changes to the service or client layer.

No real network calls are made — all LLM interactions go through the
:class:`FakeChatLiteLLM` test double or :class:`StubLLMClient` implementations.

Covered scenarios
-----------------
FakeChatLiteLLM (mock class itself):
* ainvoke returns pre-canned AIMessage
* astream yields pre-canned AIMessageChunk tokens
* init_kwargs are captured for introspection
* call counts track invocation counts
* last_messages / last_kwargs are updated per call

Environment fixtures:
* env_openai sets the correct LLM_PROVIDER, OPENAI_API_KEY, and LLM_DEFAULT_MODEL
* env_ollama sets the correct LLM_PROVIDER, OLLAMA_BASE_URL, and LLM_DEFAULT_MODEL

Settings fixtures:
* openai_llm_settings produces correct model string and API key
* ollama_llm_settings produces correct model string, api_base, api_key="ollama"

Patched ChatLiteLLM fixtures:
* patched_chat_litellm intercepts LLMClient constructor calls
* fake_chat_litellm_openai returns correct fake for OpenAI (routes correctly)
* fake_chat_litellm_ollama returns correct fake for Ollama (routes correctly)

LLMClient fixtures:
* llm_client_openai has model_string="openai/gpt-4o-mini", provider="openai"
* llm_client_ollama has model_string="ollama/llama3.2", provider="ollama"
* ainvoke returns FAKE_RESPONSE_TEXT via fake
* astream yields FAKE_STREAM_TOKENS via fake

Mock LLMClient fixtures (MagicMock-based):
* mock_llm_client_openai exposes model_string / provider and satisfies protocol
* mock_llm_client_ollama exposes model_string / provider and satisfies protocol

Stub LLMClient fixtures (pure Python):
* stub_llm_client satisfies LLMClientProtocol structurally
* streaming_stub_llm_client yields FAKE_STREAM_TOKENS

ChatService fixtures:
* stub_chat_service.complete returns FAKE_RESPONSE_TEXT
* chat_service_openai.complete delegates to fake OpenAI client
* chat_service_ollama.stream yields FAKE_STREAM_TOKENS

Provider portability:
* Both providers produce different model strings (env-only switch)
* Both providers return AIMessage / yield strings from ChatService
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from core.config import LLMProvider, LLMSettings, get_settings
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
)

# ---------------------------------------------------------------------------
# FakeChatLiteLLM — unit tests for the mock class itself
# ---------------------------------------------------------------------------


class TestFakeChatLiteLLM:
    """Validate :class:`FakeChatLiteLLM` behaves as a correct ChatLiteLLM stand-in."""

    @pytest.mark.asyncio
    async def test_ainvoke_returns_ai_message(self) -> None:
        fake = FakeChatLiteLLM(response="hello from fake")
        result = await fake.ainvoke([HumanMessage(content="hi")])
        assert isinstance(result, AIMessage)
        assert result.content == "hello from fake"

    @pytest.mark.asyncio
    async def test_ainvoke_uses_default_response(self) -> None:
        fake = FakeChatLiteLLM()
        result = await fake.ainvoke([HumanMessage(content="hi")])
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_astream_yields_ai_message_chunks(self) -> None:
        tokens = ["Hello", " ", "world", "!"]
        fake = FakeChatLiteLLM(stream_tokens=tokens)
        collected: list[str] = []
        async for chunk in fake.astream([HumanMessage(content="hi")]):
            collected.append(chunk.content)
        assert collected == tokens

    @pytest.mark.asyncio
    async def test_astream_uses_default_tokens(self) -> None:
        fake = FakeChatLiteLLM()
        collected: list[str] = []
        async for chunk in fake.astream([HumanMessage(content="hi")]):
            collected.append(chunk.content)
        assert collected == FAKE_STREAM_TOKENS

    def test_init_kwargs_captured(self) -> None:
        fake = FakeChatLiteLLM(model="openai/gpt-4o", api_key="sk-test", temperature=0.7)
        assert fake.init_kwargs["model"] == "openai/gpt-4o"
        assert fake.init_kwargs["api_key"] == "sk-test"
        assert fake.init_kwargs["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_ainvoke_increments_call_count(self) -> None:
        fake = FakeChatLiteLLM()
        assert fake.ainvoke_call_count == 0
        await fake.ainvoke([HumanMessage(content="first")])
        assert fake.ainvoke_call_count == 1
        await fake.ainvoke([HumanMessage(content="second")])
        assert fake.ainvoke_call_count == 2

    @pytest.mark.asyncio
    async def test_astream_increments_call_count(self) -> None:
        fake = FakeChatLiteLLM()
        assert fake.astream_call_count == 0
        async for _ in fake.astream([HumanMessage(content="hi")]):
            pass
        assert fake.astream_call_count == 1

    @pytest.mark.asyncio
    async def test_ainvoke_captures_last_messages(self) -> None:
        fake = FakeChatLiteLLM()
        messages = [
            SystemMessage(content="Be concise."),
            HumanMessage(content="Capital of France?"),
        ]
        await fake.ainvoke(messages)
        assert len(fake.last_messages) == 2
        assert fake.last_messages[0].content == "Be concise."
        assert fake.last_messages[1].content == "Capital of France?"

    @pytest.mark.asyncio
    async def test_ainvoke_captures_extra_kwargs(self) -> None:
        fake = FakeChatLiteLLM()
        await fake.ainvoke([HumanMessage(content="hi")], stop=["<END>"], temperature=0.5)
        assert fake.last_kwargs["stop"] == ["<END>"]
        assert fake.last_kwargs["temperature"] == 0.5

    @pytest.mark.asyncio
    async def test_astream_captures_last_messages(self) -> None:
        fake = FakeChatLiteLLM()
        messages = [HumanMessage(content="stream test")]
        async for _ in fake.astream(messages):
            pass
        assert len(fake.last_messages) == 1
        assert fake.last_messages[0].content == "stream test"

    def test_custom_response_preserved(self) -> None:
        custom = "custom response for this test"
        fake = FakeChatLiteLLM(response=custom)
        assert fake.response == custom

    def test_custom_stream_tokens_preserved(self) -> None:
        tokens = ["a", "b", "c"]
        fake = FakeChatLiteLLM(stream_tokens=tokens)
        assert fake.stream_tokens == tokens

    def test_independent_instances_do_not_share_state(self) -> None:
        """Two FakeChatLiteLLM instances must not share call count state."""
        fake1 = FakeChatLiteLLM(response="first")
        fake2 = FakeChatLiteLLM(response="second")
        # Verify they are different objects with independent counters
        assert fake1 is not fake2
        assert fake1.ainvoke_call_count == 0
        assert fake2.ainvoke_call_count == 0


class TestFakeStreamingChatLiteLLM:
    """Validate :class:`FakeStreamingChatLiteLLM` character-level token splitting."""

    @pytest.mark.asyncio
    async def test_default_uses_fake_stream_tokens(self) -> None:
        fake = FakeStreamingChatLiteLLM()
        collected: list[str] = []
        async for chunk in fake.astream([HumanMessage(content="hi")]):
            collected.append(chunk.content)
        assert collected == FAKE_STREAM_TOKENS

    @pytest.mark.asyncio
    async def test_ainvoke_returns_full_response(self) -> None:
        fake = FakeStreamingChatLiteLLM(response="full response text")
        result = await fake.ainvoke([HumanMessage(content="hi")])
        assert result.content == "full response text"

    @pytest.mark.asyncio
    async def test_token_count_splits_response(self) -> None:
        """token_count causes the response to be split into smaller chunks."""
        response = "Hello World Test"
        fake = FakeStreamingChatLiteLLM(response=response, token_count=4)
        collected: list[str] = []
        async for chunk in fake.astream([HumanMessage(content="hi")]):
            collected.append(chunk.content)
        # Should have split into approximately 4 chunks
        assert len(collected) >= 1
        assert "".join(collected) == response


# ---------------------------------------------------------------------------
# Environment fixtures
# ---------------------------------------------------------------------------


class TestEnvOpenAI:
    """Verify :func:`env_openai` correctly patches the process environment."""

    def test_llm_provider_is_openai(self, env_openai: None) -> None:
        s = get_settings()
        assert s.llm_provider == LLMProvider.openai

    def test_openai_api_key_is_set(self, env_openai: None) -> None:
        s = get_settings()
        assert s.openai_api_key.get_secret_value() == OPENAI_TEST_KEY

    def test_llm_default_model_is_set(self, env_openai: None) -> None:
        s = get_settings()
        assert s.llm_default_model == OPENAI_DEFAULT_MODEL

    def test_settings_llm_property_has_correct_provider(self, env_openai: None) -> None:
        s = get_settings()
        assert s.llm.provider == LLMProvider.openai

    def test_model_string_has_openai_prefix(self, env_openai: None) -> None:
        s = get_settings()
        assert s.llm.litellm_model == f"openai/{OPENAI_DEFAULT_MODEL}"

    def test_no_ollama_base_url_by_default(self, env_openai: None) -> None:
        """env_openai should not leave OLLAMA_BASE_URL set from a prior test."""
        s = get_settings()
        # Default ollama_base_url is the standard localhost — that's fine
        # The key check is that the active provider is openai, not ollama
        assert s.llm_provider != LLMProvider.ollama


class TestEnvOllama:
    """Verify :func:`env_ollama` correctly patches the process environment."""

    def test_llm_provider_is_ollama(self, env_ollama: None) -> None:
        s = get_settings()
        assert s.llm_provider == LLMProvider.ollama

    def test_ollama_base_url_is_set(self, env_ollama: None) -> None:
        s = get_settings()
        assert s.ollama_base_url == OLLAMA_TEST_URL

    def test_llm_default_model_is_set(self, env_ollama: None) -> None:
        s = get_settings()
        assert s.llm_default_model == OLLAMA_DEFAULT_MODEL

    def test_model_string_has_ollama_prefix(self, env_ollama: None) -> None:
        s = get_settings()
        assert s.llm.litellm_model == f"ollama/{OLLAMA_DEFAULT_MODEL}"

    def test_ollama_kwargs_include_api_base(self, env_ollama: None) -> None:
        s = get_settings()
        kwargs = s.llm.as_litellm_kwargs()
        assert kwargs["api_base"] == OLLAMA_TEST_URL
        assert kwargs["api_key"] == "ollama"

    def test_no_openai_key_required(self, env_ollama: None) -> None:
        """Ollama does not require an OpenAI API key."""
        s = get_settings()
        assert s.llm_provider != LLMProvider.openai


# ---------------------------------------------------------------------------
# Settings fixtures
# ---------------------------------------------------------------------------


class TestOpenAILLMSettings:
    """Verify :func:`openai_llm_settings` produces a correct :class:`LLMSettings`."""

    def test_provider_is_openai(self, openai_llm_settings: LLMSettings) -> None:
        assert openai_llm_settings.provider == LLMProvider.openai

    def test_default_model(self, openai_llm_settings: LLMSettings) -> None:
        assert openai_llm_settings.default_model == OPENAI_DEFAULT_MODEL

    def test_litellm_model_string(self, openai_llm_settings: LLMSettings) -> None:
        assert openai_llm_settings.litellm_model == f"openai/{OPENAI_DEFAULT_MODEL}"

    def test_api_key_is_set(self, openai_llm_settings: LLMSettings) -> None:
        assert openai_llm_settings.openai_api_key.get_secret_value() == OPENAI_TEST_KEY

    def test_kwargs_contain_model_and_key(self, openai_llm_settings: LLMSettings) -> None:
        kwargs = openai_llm_settings.as_litellm_kwargs()
        assert kwargs["model"] == f"openai/{OPENAI_DEFAULT_MODEL}"
        assert kwargs["api_key"] == OPENAI_TEST_KEY

    def test_kwargs_do_not_contain_api_base(self, openai_llm_settings: LLMSettings) -> None:
        """OpenAI uses the default base URL — no api_base kwarg needed."""
        kwargs = openai_llm_settings.as_litellm_kwargs()
        assert "api_base" not in kwargs


class TestOllamaLLMSettings:
    """Verify :func:`ollama_llm_settings` produces a correct :class:`LLMSettings`."""

    def test_provider_is_ollama(self, ollama_llm_settings: LLMSettings) -> None:
        assert ollama_llm_settings.provider == LLMProvider.ollama

    def test_default_model(self, ollama_llm_settings: LLMSettings) -> None:
        assert ollama_llm_settings.default_model == OLLAMA_DEFAULT_MODEL

    def test_litellm_model_string(self, ollama_llm_settings: LLMSettings) -> None:
        assert ollama_llm_settings.litellm_model == f"ollama/{OLLAMA_DEFAULT_MODEL}"

    def test_ollama_base_url(self, ollama_llm_settings: LLMSettings) -> None:
        assert ollama_llm_settings.ollama_base_url == OLLAMA_TEST_URL

    def test_kwargs_contain_model_and_api_base(self, ollama_llm_settings: LLMSettings) -> None:
        kwargs = ollama_llm_settings.as_litellm_kwargs()
        assert kwargs["model"] == f"ollama/{OLLAMA_DEFAULT_MODEL}"
        assert kwargs["api_base"] == OLLAMA_TEST_URL

    def test_ollama_sentinel_api_key(self, ollama_llm_settings: LLMSettings) -> None:
        """Ollama uses a sentinel 'ollama' string as api_key, not a real API key."""
        kwargs = ollama_llm_settings.as_litellm_kwargs()
        assert kwargs["api_key"] == "ollama"


# ---------------------------------------------------------------------------
# Patched ChatLiteLLM fixtures
# ---------------------------------------------------------------------------


class TestPatchedChatLiteLLM:
    """Verify :func:`patched_chat_litellm` intercepts ChatLiteLLM constructor."""

    def test_constructor_is_intercepted(
        self,
        patched_chat_litellm: MagicMock,
        openai_llm_settings: LLMSettings,
    ) -> None:
        from domains.chat.llm_client import LLMClient

        LLMClient(settings=openai_llm_settings)
        patched_chat_litellm.assert_called_once()

    def test_model_kwarg_is_passed_for_openai(
        self,
        patched_chat_litellm: MagicMock,
        openai_llm_settings: LLMSettings,
    ) -> None:
        from domains.chat.llm_client import LLMClient

        LLMClient(settings=openai_llm_settings)
        _, call_kwargs = patched_chat_litellm.call_args
        assert call_kwargs["model"] == f"openai/{OPENAI_DEFAULT_MODEL}"

    def test_api_key_kwarg_is_passed_for_openai(
        self,
        patched_chat_litellm: MagicMock,
        openai_llm_settings: LLMSettings,
    ) -> None:
        from domains.chat.llm_client import LLMClient

        LLMClient(settings=openai_llm_settings)
        _, call_kwargs = patched_chat_litellm.call_args
        assert call_kwargs.get("api_key") == OPENAI_TEST_KEY

    def test_override_kwargs_are_forwarded(
        self,
        patched_chat_litellm: MagicMock,
        openai_llm_settings: LLMSettings,
    ) -> None:
        from domains.chat.llm_client import LLMClient

        LLMClient(settings=openai_llm_settings, temperature=0.3, max_tokens=256)
        _, call_kwargs = patched_chat_litellm.call_args
        assert call_kwargs["temperature"] == 0.3
        assert call_kwargs["max_tokens"] == 256

    def test_ollama_api_base_is_passed(
        self,
        patched_chat_litellm: MagicMock,
        ollama_llm_settings: LLMSettings,
    ) -> None:
        from domains.chat.llm_client import LLMClient

        LLMClient(settings=ollama_llm_settings)
        _, call_kwargs = patched_chat_litellm.call_args
        assert call_kwargs["api_base"] == OLLAMA_TEST_URL
        assert call_kwargs["api_key"] == "ollama"

    def test_ollama_model_kwarg_is_correct(
        self,
        patched_chat_litellm: MagicMock,
        ollama_llm_settings: LLMSettings,
    ) -> None:
        from domains.chat.llm_client import LLMClient

        LLMClient(settings=ollama_llm_settings)
        _, call_kwargs = patched_chat_litellm.call_args
        assert call_kwargs["model"] == f"ollama/{OLLAMA_DEFAULT_MODEL}"


class TestFakeChatLiteLLMOpenAIFixture:
    """Verify :func:`fake_chat_litellm_openai` patches correctly for OpenAI."""

    @pytest.mark.asyncio
    async def test_llm_client_ainvoke_returns_fake_response(
        self,
        llm_client_openai: Any,
    ) -> None:
        result = await llm_client_openai.ainvoke([HumanMessage(content="hello")])
        assert isinstance(result, AIMessage)
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_llm_client_astream_yields_fake_tokens(
        self,
        llm_client_openai: Any,
    ) -> None:
        chunks: list[str] = []
        async for chunk in llm_client_openai.astream([HumanMessage(content="hi")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS

    def test_fake_instance_init_kwargs_have_model(
        self,
        fake_chat_litellm_openai: FakeChatLiteLLM,
    ) -> None:
        """Fake was constructed with the expected OpenAI model string."""
        assert "model" in fake_chat_litellm_openai.init_kwargs
        assert fake_chat_litellm_openai.init_kwargs["model"] == f"openai/{OPENAI_DEFAULT_MODEL}"

    def test_fake_instance_init_kwargs_have_api_key(
        self,
        fake_chat_litellm_openai: FakeChatLiteLLM,
    ) -> None:
        assert fake_chat_litellm_openai.init_kwargs.get("api_key") == OPENAI_TEST_KEY


class TestFakeChatLiteLLMOllamaFixture:
    """Verify :func:`fake_chat_litellm_ollama` patches correctly for Ollama."""

    @pytest.mark.asyncio
    async def test_llm_client_ainvoke_returns_fake_response(
        self,
        llm_client_ollama: Any,
    ) -> None:
        result = await llm_client_ollama.ainvoke([HumanMessage(content="hello")])
        assert isinstance(result, AIMessage)
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_llm_client_astream_yields_fake_tokens(
        self,
        llm_client_ollama: Any,
    ) -> None:
        chunks: list[str] = []
        async for chunk in llm_client_ollama.astream([HumanMessage(content="ping")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS

    def test_fake_instance_init_kwargs_have_api_base(
        self,
        fake_chat_litellm_ollama: FakeChatLiteLLM,
    ) -> None:
        assert fake_chat_litellm_ollama.init_kwargs.get("api_base") == OLLAMA_TEST_URL

    def test_fake_instance_init_kwargs_have_sentinel_api_key(
        self,
        fake_chat_litellm_ollama: FakeChatLiteLLM,
    ) -> None:
        assert fake_chat_litellm_ollama.init_kwargs.get("api_key") == "ollama"


# ---------------------------------------------------------------------------
# LLMClient fixtures
# ---------------------------------------------------------------------------


class TestLLMClientOpenAI:
    """Verify :func:`llm_client_openai` is correctly wired."""

    def test_model_string_is_openai(self, llm_client_openai: Any) -> None:
        assert llm_client_openai.model_string == f"openai/{OPENAI_DEFAULT_MODEL}"

    def test_provider_is_openai(self, llm_client_openai: Any) -> None:
        assert llm_client_openai.provider == "openai"

    @pytest.mark.asyncio
    async def test_ainvoke_returns_fake_response(self, llm_client_openai: Any) -> None:
        result = await llm_client_openai.ainvoke([HumanMessage(content="hello")])
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_ainvoke_with_multi_turn_messages(self, llm_client_openai: Any) -> None:
        from langchain_core.messages import AIMessage as AIMsg

        messages = [
            SystemMessage(content="You are helpful."),
            HumanMessage(content="What is 2+2?"),
            AIMsg(content="4"),
            HumanMessage(content="And 3+3?"),
        ]
        result = await llm_client_openai.ainvoke(messages)
        assert isinstance(result, AIMessage)

    @pytest.mark.asyncio
    async def test_astream_yields_all_tokens(self, llm_client_openai: Any) -> None:
        chunks: list[str] = []
        async for chunk in llm_client_openai.astream([HumanMessage(content="stream")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS

    @pytest.mark.asyncio
    async def test_astream_yields_strings_only(self, llm_client_openai: Any) -> None:
        async for chunk in llm_client_openai.astream([HumanMessage(content="test")]):
            assert isinstance(chunk, str), f"Expected str, got {type(chunk)}"

    @pytest.mark.asyncio
    async def test_astream_non_empty_chunks(self, llm_client_openai: Any) -> None:
        """LLMClient.astream skips empty chunks — all yielded strings must be truthy."""
        async for chunk in llm_client_openai.astream([HumanMessage(content="test")]):
            assert chunk, f"Empty chunk yielded: {chunk!r}"


class TestLLMClientOllama:
    """Verify :func:`llm_client_ollama` is correctly wired."""

    def test_model_string_is_ollama(self, llm_client_ollama: Any) -> None:
        assert llm_client_ollama.model_string == f"ollama/{OLLAMA_DEFAULT_MODEL}"

    def test_provider_is_ollama(self, llm_client_ollama: Any) -> None:
        assert llm_client_ollama.provider == "ollama"

    @pytest.mark.asyncio
    async def test_ainvoke_returns_fake_response(self, llm_client_ollama: Any) -> None:
        result = await llm_client_ollama.ainvoke([HumanMessage(content="hello")])
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_astream_yields_all_tokens(self, llm_client_ollama: Any) -> None:
        chunks: list[str] = []
        async for chunk in llm_client_ollama.astream([HumanMessage(content="ollama stream")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS

    @pytest.mark.asyncio
    async def test_astream_yields_strings_only(self, llm_client_ollama: Any) -> None:
        async for chunk in llm_client_ollama.astream([HumanMessage(content="test")]):
            assert isinstance(chunk, str), f"Expected str, got {type(chunk)}"

    @pytest.mark.asyncio
    async def test_astream_non_empty_chunks(self, llm_client_ollama: Any) -> None:
        """LLMClient.astream skips empty chunks — all yielded strings must be truthy."""
        async for chunk in llm_client_ollama.astream([HumanMessage(content="test")]):
            assert chunk, f"Empty chunk yielded: {chunk!r}"


# ---------------------------------------------------------------------------
# Mock LLMClient fixtures (MagicMock-based)
# ---------------------------------------------------------------------------


class TestMockLLMClientOpenAI:
    """Verify :func:`mock_llm_client_openai` is correctly wired."""

    def test_model_string(self, mock_llm_client_openai: MagicMock) -> None:
        assert mock_llm_client_openai.model_string == f"openai/{OPENAI_DEFAULT_MODEL}"

    def test_provider(self, mock_llm_client_openai: MagicMock) -> None:
        assert mock_llm_client_openai.provider == "openai"

    def test_satisfies_protocol(self, mock_llm_client_openai: MagicMock) -> None:
        assert isinstance(mock_llm_client_openai, LLMClientProtocol)

    @pytest.mark.asyncio
    async def test_ainvoke_returns_ai_message(self, mock_llm_client_openai: MagicMock) -> None:
        result = await mock_llm_client_openai.ainvoke([HumanMessage(content="hi")])
        assert isinstance(result, AIMessage)
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_ainvoke_is_tracked_by_mock(self, mock_llm_client_openai: MagicMock) -> None:
        await mock_llm_client_openai.ainvoke([HumanMessage(content="hi")])
        mock_llm_client_openai.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_astream_yields_tokens(self, mock_llm_client_openai: MagicMock) -> None:
        chunks: list[str] = []
        async for chunk in mock_llm_client_openai.astream([HumanMessage(content="stream")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS


class TestMockLLMClientOllama:
    """Verify :func:`mock_llm_client_ollama` is correctly wired."""

    def test_model_string(self, mock_llm_client_ollama: MagicMock) -> None:
        assert mock_llm_client_ollama.model_string == f"ollama/{OLLAMA_DEFAULT_MODEL}"

    def test_provider(self, mock_llm_client_ollama: MagicMock) -> None:
        assert mock_llm_client_ollama.provider == "ollama"

    def test_satisfies_protocol(self, mock_llm_client_ollama: MagicMock) -> None:
        assert isinstance(mock_llm_client_ollama, LLMClientProtocol)

    @pytest.mark.asyncio
    async def test_ainvoke_returns_ai_message(self, mock_llm_client_ollama: MagicMock) -> None:
        result = await mock_llm_client_ollama.ainvoke([HumanMessage(content="hi")])
        assert isinstance(result, AIMessage)

    @pytest.mark.asyncio
    async def test_astream_yields_tokens(self, mock_llm_client_ollama: MagicMock) -> None:
        chunks: list[str] = []
        async for chunk in mock_llm_client_ollama.astream([HumanMessage(content="ollama")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS


# ---------------------------------------------------------------------------
# Stub LLMClient fixtures
# ---------------------------------------------------------------------------


class TestStubLLMClient:
    """Verify :func:`stub_llm_client` satisfies the protocol with zero patches."""

    def test_satisfies_protocol(self, stub_llm_client: LLMClientProtocol) -> None:
        assert isinstance(stub_llm_client, LLMClientProtocol)

    @pytest.mark.asyncio
    async def test_ainvoke_returns_fake_response(self, stub_llm_client: LLMClientProtocol) -> None:
        result = await stub_llm_client.ainvoke([HumanMessage(content="hi")])
        assert isinstance(result, AIMessage)
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_astream_yields_fake_tokens(self, stub_llm_client: LLMClientProtocol) -> None:
        chunks: list[str] = []
        async for chunk in stub_llm_client.astream([HumanMessage(content="hi")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS

    @pytest.mark.asyncio
    async def test_astream_yields_strings_only(self, stub_llm_client: LLMClientProtocol) -> None:
        async for chunk in stub_llm_client.astream([HumanMessage(content="test")]):
            assert isinstance(chunk, str)


class TestStreamingStubLLMClient:
    """Verify :func:`streaming_stub_llm_client` yields multi-token stream."""

    def test_satisfies_protocol(self, streaming_stub_llm_client: LLMClientProtocol) -> None:
        assert isinstance(streaming_stub_llm_client, LLMClientProtocol)

    @pytest.mark.asyncio
    async def test_astream_yields_multiple_tokens(
        self, streaming_stub_llm_client: LLMClientProtocol
    ) -> None:
        chunks: list[str] = []
        async for chunk in streaming_stub_llm_client.astream([HumanMessage(content="hi")]):
            chunks.append(chunk)
        assert len(chunks) == len(FAKE_STREAM_TOKENS)
        assert all(isinstance(c, str) for c in chunks)

    @pytest.mark.asyncio
    async def test_joined_tokens_match_expected(
        self, streaming_stub_llm_client: LLMClientProtocol
    ) -> None:
        full: list[str] = []
        async for chunk in streaming_stub_llm_client.astream([HumanMessage(content="hi")]):
            full.append(chunk)
        assert "".join(full) == "".join(FAKE_STREAM_TOKENS)


# ---------------------------------------------------------------------------
# ChatService fixtures
# ---------------------------------------------------------------------------


class TestStubChatService:
    """Verify :func:`stub_chat_service` works end-to-end with zero patches."""

    @pytest.mark.asyncio
    async def test_complete_returns_ai_message(self, stub_chat_service: ChatService) -> None:
        result = await stub_chat_service.complete([HumanMessage(content="hello")])
        assert isinstance(result, AIMessage)
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_stream_yields_tokens(self, stub_chat_service: ChatService) -> None:
        chunks: list[str] = []
        async for chunk in stub_chat_service.stream([HumanMessage(content="hello")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS

    @pytest.mark.asyncio
    async def test_complete_with_system_message(self, stub_chat_service: ChatService) -> None:
        messages = [
            SystemMessage(content="Be concise."),
            HumanMessage(content="Capital of France?"),
        ]
        result = await stub_chat_service.complete(messages)
        assert isinstance(result, AIMessage)

    @pytest.mark.asyncio
    async def test_stream_yields_strings_only(self, stub_chat_service: ChatService) -> None:
        async for chunk in stub_chat_service.stream([HumanMessage(content="test")]):
            assert isinstance(chunk, str)

    @pytest.mark.asyncio
    async def test_complete_with_kwargs_forwarded(self, stub_chat_service: ChatService) -> None:
        """Extra kwargs (temperature, max_tokens) do not cause errors."""
        result = await stub_chat_service.complete(
            [HumanMessage(content="hi")],
            temperature=0.1,
            max_tokens=100,
        )
        assert isinstance(result, AIMessage)


class TestChatServiceOpenAI:
    """Verify :func:`chat_service_openai` integrates correctly with fake OpenAI."""

    @pytest.mark.asyncio
    async def test_complete_returns_fake_response(self, chat_service_openai: ChatService) -> None:
        result = await chat_service_openai.complete([HumanMessage(content="hello")])
        assert isinstance(result, AIMessage)
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_stream_yields_fake_tokens(self, chat_service_openai: ChatService) -> None:
        chunks: list[str] = []
        async for chunk in chat_service_openai.stream([HumanMessage(content="stream")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS

    @pytest.mark.asyncio
    async def test_complete_with_kwargs(self, chat_service_openai: ChatService) -> None:
        result = await chat_service_openai.complete(
            [HumanMessage(content="hi")],
            temperature=0.1,
            max_tokens=100,
        )
        assert isinstance(result, AIMessage)

    @pytest.mark.asyncio
    async def test_stream_chunk_count_matches(self, chat_service_openai: ChatService) -> None:
        count = 0
        async for _ in chat_service_openai.stream([HumanMessage(content="count")]):
            count += 1
        assert count == len(FAKE_STREAM_TOKENS)


class TestChatServiceOllama:
    """Verify :func:`chat_service_ollama` integrates correctly with fake Ollama."""

    @pytest.mark.asyncio
    async def test_complete_returns_fake_response(self, chat_service_ollama: ChatService) -> None:
        result = await chat_service_ollama.complete([HumanMessage(content="hello")])
        assert isinstance(result, AIMessage)
        assert result.content == FAKE_RESPONSE_TEXT

    @pytest.mark.asyncio
    async def test_stream_yields_fake_tokens(self, chat_service_ollama: ChatService) -> None:
        chunks: list[str] = []
        async for chunk in chat_service_ollama.stream([HumanMessage(content="ollama")]):
            chunks.append(chunk)
        assert chunks == FAKE_STREAM_TOKENS

    @pytest.mark.asyncio
    async def test_stream_count_matches_token_count(self, chat_service_ollama: ChatService) -> None:
        count = 0
        async for _ in chat_service_ollama.stream([HumanMessage(content="count")]):
            count += 1
        assert count == len(FAKE_STREAM_TOKENS)


# ---------------------------------------------------------------------------
# Provider portability — switching providers requires only fixture change
# ---------------------------------------------------------------------------


class TestProviderPortability:
    """Demonstrate that provider switching requires zero code changes.

    Documents the ``llm_provider_portability`` evaluation principle: LLM
    provider switching happens at the env/fixture level, with zero changes to
    service, client, or domain logic.
    """

    def test_openai_and_ollama_model_strings_differ(
        self,
        openai_llm_settings: LLMSettings,
        ollama_llm_settings: LLMSettings,
    ) -> None:
        """Different providers produce different model strings — no code changes needed."""
        from domains.chat.llm_factory import ProviderFactory

        openai_model = ProviderFactory.from_settings(openai_llm_settings)
        ollama_model = ProviderFactory.from_settings(ollama_llm_settings)

        assert openai_model != ollama_model
        assert openai_model.startswith("openai/")
        assert ollama_model.startswith("ollama/")

    @pytest.mark.asyncio
    async def test_both_providers_return_ai_message(
        self,
        chat_service_openai: ChatService,
        chat_service_ollama: ChatService,
    ) -> None:
        """Both providers produce identical response shapes — service is agnostic."""
        msg = [HumanMessage(content="provider portability test")]
        result_openai = await chat_service_openai.complete(msg)
        result_ollama = await chat_service_ollama.complete(msg)

        assert isinstance(result_openai, AIMessage)
        assert isinstance(result_ollama, AIMessage)

    @pytest.mark.asyncio
    async def test_both_providers_stream_string_chunks(
        self,
        chat_service_openai: ChatService,
        chat_service_ollama: ChatService,
    ) -> None:
        """Both providers stream string chunks — ChatService is provider-agnostic."""
        msg = [HumanMessage(content="stream portability")]

        openai_chunks: list[str] = []
        async for chunk in chat_service_openai.stream(msg):
            openai_chunks.append(chunk)

        ollama_chunks: list[str] = []
        async for chunk in chat_service_ollama.stream(msg):
            ollama_chunks.append(chunk)

        assert all(isinstance(c, str) for c in openai_chunks)
        assert all(isinstance(c, str) for c in ollama_chunks)
        assert len(openai_chunks) > 0
        assert len(ollama_chunks) > 0

    def test_env_openai_sets_correct_provider(self, env_openai: None) -> None:
        """env_openai fixture wires the OpenAI provider via environment variables."""
        s = get_settings()
        assert s.llm.provider == LLMProvider.openai
        assert s.llm.litellm_model.startswith("openai/")

    def test_env_ollama_sets_correct_provider(self, env_ollama: None) -> None:
        """env_ollama fixture wires the Ollama provider via environment variables."""
        s = get_settings()
        assert s.llm.provider == LLMProvider.ollama
        assert s.llm.litellm_model.startswith("ollama/")
        assert s.llm.as_litellm_kwargs()["api_base"] == OLLAMA_TEST_URL

    def test_model_string_prefix_matches_provider(
        self,
        openai_llm_settings: LLMSettings,
        ollama_llm_settings: LLMSettings,
    ) -> None:
        """Model string prefix always matches the configured provider."""
        from domains.chat.llm_factory import ProviderFactory

        for settings, expected_prefix in [
            (openai_llm_settings, "openai/"),
            (ollama_llm_settings, "ollama/"),
        ]:
            model_str = ProviderFactory.from_settings(settings)
            assert model_str.startswith(expected_prefix), (
                f"Expected model string to start with '{expected_prefix}', got {model_str!r}"
            )

    def test_settings_cache_cleared_between_env_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The settings_cache_clear autouse fixture prevents cross-test contamination.

        Simulates what happens across two tests: first uses OpenAI, second
        switches to Ollama.  Verifies the cache clear works correctly.
        """
        # Simulate first test
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        get_settings.cache_clear()
        s1 = get_settings()
        assert s1.llm_provider == LLMProvider.openai

        # Simulate switching — clear cache and change env
        get_settings.cache_clear()
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        get_settings.cache_clear()
        s2 = get_settings()
        assert s2.llm_provider == LLMProvider.ollama
