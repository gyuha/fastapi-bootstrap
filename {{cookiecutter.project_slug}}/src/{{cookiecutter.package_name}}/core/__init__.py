"""Core infrastructure: security, logging, cache, middleware.

Public re-exports for convenience::

    from {{ cookiecutter.package_name }}.core import settings
    from {{ cookiecutter.package_name }}.core import get_settings
"""

from {{ cookiecutter.package_name }}.core.config import Settings, get_settings, settings

{% if cookiecutter.include_chat_domain == "yes" %}
from {{ cookiecutter.package_name }}.core.config import LLMProvider, LLMSettings
{% endif %}

__all__ = [
    "Settings",
    "get_settings",
    "settings",
{% if cookiecutter.include_chat_domain == "yes" %}
    "LLMProvider",
    "LLMSettings",
{% endif %}
]
