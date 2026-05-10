"""ASGI middleware for the application.

CorrelationIdMiddleware
-----------------------
Injects a ``X-Correlation-ID`` header into every request/response pair and
binds the ID to structlog's context-var store so that all log statements
emitted during the request lifecycle carry ``correlation_id``.

If the incoming request already carries ``X-Correlation-ID``, that value is
reused (useful when chaining services or replaying test requests).  Otherwise
a new UUIDv4 is generated.

Usage (registered automatically by ``create_app``)::

    app.add_middleware(CorrelationIdMiddleware)
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)

#: Header name used to propagate the correlation ID.
CORRELATION_ID_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Inject and propagate a ``X-Correlation-ID`` on every request.

    Behaviour:
    1. Read ``X-Correlation-ID`` from the incoming request headers.
    2. If absent, generate a new ``uuid4`` string.
    3. Bind the ID to structlog's context-vars store for the duration of the
       request (automatically cleared after the response is sent).
    4. Add ``X-Correlation-ID`` to the response headers.
    5. Log every request/response pair at INFO level with timing, method,
       path, and status code.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or str(uuid.uuid4())

        # Bind to structlog context-vars so all loggers in this request see it
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            method=request.method,
            path=request.url.path,
        )

        logger.info(
            "request_started",
            client=_client_ip(request),
            query_string=str(request.url.query) or None,
        )

        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_error")
            raise
        finally:
            structlog.contextvars.clear_contextvars()

        response.headers[CORRELATION_ID_HEADER] = correlation_id
        logger.info(
            "request_finished",
            status_code=response.status_code,
        )

        return response


def _client_ip(request: Request) -> str | None:
    """Extract the real client IP, honouring common proxy headers."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None
