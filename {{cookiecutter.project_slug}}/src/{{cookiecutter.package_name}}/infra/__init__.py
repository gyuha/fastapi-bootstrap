"""Infrastructure layer — concrete adapters for external services.

This package contains adapter implementations that bridge the application
domain to external libraries and services.  Domain code depends only on
abstract ports (interfaces); infrastructure provides the concrete wiring.

Sub-packages
------------
* ``llm/`` — LangChain / LiteLLM adapter factory for LLM provider routing.
"""
