"""structlog configuration for the application.

Provides two render modes:
  * ``json``    — machine-readable JSON lines (production / staging)
  * ``console`` — human-readable coloured output (local development)

Usage::

    from core.logging import configure_logging

    configure_logging(level="INFO", fmt="json")

The ``configure_logging`` function is called **once** in the application
lifespan so that all subsequent ``structlog.get_logger()`` calls share the
same configuration.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog and the stdlib ``logging`` root logger.

    Parameters
    ----------
    level:
        Logging level string (``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``).
    fmt:
        Output format.  ``"json"`` emits JSON lines; ``"console"`` emits
        coloured human-readable output.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # ── Shared processors (run regardless of renderer) ────────────────────────
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if fmt == "console":
        # Human-readable coloured output for local development
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
        # Include exception tracebacks as formatted strings
        shared_processors.append(structlog.dev.set_exc_info)
    else:
        # JSON output — format exceptions as a dict for log aggregators
        renderer = structlog.processors.JSONRenderer()
        shared_processors.append(structlog.processors.format_exc_info)

    structlog.configure(
        processors=[
            *shared_processors,
            # Prepare for stdlib logging hand-off (adds positional args etc.)
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ── Wire stdlib logging through structlog's ProcessorFormatter ────────────
    formatter = structlog.stdlib.ProcessorFormatter(
        # Processors that run **after** stdlib passes the record to structlog
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Silence noisy third-party loggers
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
