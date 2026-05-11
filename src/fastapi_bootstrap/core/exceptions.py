"""Application-wide HTTP exception handlers.

Registers handlers on the FastAPI ``application`` instance for:

* :class:`fastapi.HTTPException`        — returns JSON with ``detail`` field
* :class:`fastapi.RequestValidationError` — returns 422 with structured errors
* :class:`Exception`                     — 500 with a safe error message

All responses include the ``X-Correlation-ID`` header when it was propagated
by :class:`~fastapi_bootstrap.core.middleware.CorrelationIdMiddleware`.

Usage::

    from fastapi_bootstrap.core.exceptions import register_exception_handlers

    register_exception_handlers(app)
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from fastapi_bootstrap.core.middleware import CORRELATION_ID_HEADER

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _error_response(
    request: Request,
    status_code: int,
    detail: object,
) -> JSONResponse:
    """Build a JSON error response that includes the correlation ID header."""
    correlation_id = request.headers.get(CORRELATION_ID_HEADER, "")
    headers = {CORRELATION_ID_HEADER: correlation_id} if correlation_id else {}
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)  # noqa: S101
    logger.warning(
        "http_exception",
        status_code=exc.status_code,
        detail=exc.detail,
    )
    return _error_response(request, exc.status_code, exc.detail)


async def _validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    errors = exc.errors()
    logger.warning("validation_error", errors=errors)
    return _error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        errors,
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", exc_type=type(exc).__name__)
    return _error_response(
        request,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Internal server error.",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_exception_handlers(application: FastAPI) -> None:
    """Register all exception handlers on *application*."""
    application.add_exception_handler(HTTPException, _http_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(RequestValidationError, _validation_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(Exception, _unhandled_exception_handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Re-usable domain exceptions
# ---------------------------------------------------------------------------


class AppError(Exception):
    """Base class for application-level errors.

    Domain services should raise subclasses of this.  The caller (router or
    service layer) is responsible for converting these into HTTP responses.
    """

    def __init__(self, message: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NotFoundError(AppError):
    """Resource not found."""

    def __init__(self, resource: str = "Resource") -> None:
        super().__init__(f"{resource} not found.", status.HTTP_404_NOT_FOUND)


class ConflictError(AppError):
    """Resource already exists / state conflict."""

    def __init__(self, message: str = "Conflict.") -> None:
        super().__init__(message, status.HTTP_409_CONFLICT)


class UnauthorizedError(AppError):
    """Authentication required or token invalid."""

    def __init__(self, message: str = "Unauthorized.") -> None:
        super().__init__(message, status.HTTP_401_UNAUTHORIZED)


class ForbiddenError(AppError):
    """Caller is authenticated but lacks required permission."""

    def __init__(self, message: str = "Forbidden.") -> None:
        super().__init__(message, status.HTTP_403_FORBIDDEN)
