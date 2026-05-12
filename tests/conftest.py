"""Root-level pytest conftest for the FastAPI Bootstrap test suite.

Shared fixtures available across all test modules:

* :func:`settings_cache_clear` — automatically clears :func:`get_settings`
  LRU cache before and after every test so monkeypatched env vars don't leak
  across tests.

* :func:`env_openai` / :func:`env_ollama` — monkeypatched environment
  variable sets for each LLM provider.  These are defined in
  ``tests/chat/conftest.py`` and may be referenced by chat test modules.

No network calls, DB connections, or Redis connections are made in this file.
"""

from __future__ import annotations

import pytest

from core.config import get_settings

# ---------------------------------------------------------------------------
# Settings cache isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def settings_cache_clear() -> None:  # type: ignore[misc]
    """Clear :func:`get_settings` LRU cache before and after every test.

    :func:`~app.core.config.get_settings` is decorated with
    ``@lru_cache(maxsize=1)``.  Without cache invalidation, one test's
    ``monkeypatch.setenv`` calls bleed into the next test because the cached
    :class:`~app.core.config.Settings` object was built from the
    previous test's environment.

    This fixture runs automatically for **every** test (``autouse=True``).

    Usage in test modules (implicit — no explicit request needed)::

        def test_something(monkeypatch):
            monkeypatch.setenv("LLM_PROVIDER", "ollama")
            # Settings are re-read from the current environment — no stale cache
            from core.config import get_settings
            s = get_settings()
            assert s.llm_provider.value == "ollama"
    """
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()
