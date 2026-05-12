"""LLM infrastructure adapters.

Provides the :func:`~app.infra.llm.provider_factory.make_chat_litellm`
factory for creating :class:`langchain_litellm.ChatLiteLLM` instances
from application settings.

This is the **only** place in the application codebase that imports
``langchain_litellm.ChatLiteLLM`` directly.  All domain code receives
a :class:`~app.domains.chat.ports.LLMClientProtocol` abstraction
instead.
"""
