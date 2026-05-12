"""Package entry point for direct module execution.

Allows running the application as::

    uv run python -m app                          # dev — reload ON
    APP_ENV=production uv run python -m app       # prod — reload OFF

This file delegates entirely to the ``__main__`` block in :mod:`.main`
so the two execution methods stay in sync:

* ``uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload``
  — explicit uvicorn invocation (used by ``make dev`` and ``make serve``)
* ``uv run python -m app``
  — package-level entry point (this file)
"""

from __future__ import annotations

from pathlib import Path

import uvicorn

from core.config import settings

if __name__ == "__main__":
    # Resolve the package source directory so --reload-dir is scoped correctly.
    # Path(__file__) → src/app/__main__.py
    # .parent         → src/app/
    _src_dir = Path(__file__).parent

    _reload = settings.is_development()

    uvicorn.run(
        "\1",
        host=settings.host,
        port=settings.port,
        reload=_reload,
        # Scope file-watching to the package source tree; avoids spurious
        # reloads triggered by test output, htmlcov, or .env changes.
        reload_dirs=[str(_src_dir)] if _reload else None,
        log_level=settings.log_level.lower(),
        # reload and workers > 1 are mutually exclusive in uvicorn
        workers=1 if _reload else settings.workers,
    )
