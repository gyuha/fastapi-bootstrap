"""Shared domain event abstractions.

Provides a lightweight, in-process domain-event bus for decoupled cross-domain
communication.  Events flow **within** the monolith process — they are NOT
published to an external message broker (that is out of scope per the Seed
constraints, which explicitly exclude background job queues).

Design
------
* :class:`DomainEvent` — immutable data-carrier with a unique event ID and
  timestamp.  Subclass it to model meaningful domain facts
  (e.g. ``UserSignedUp``, ``MessageCreated``).
* :class:`DomainEventBus` — singleton in-process fanout bus.  Handlers are
  registered per event type; dispatch is synchronous (``asyncio.gather``).
* :func:`publish` / :func:`subscribe` — module-level helpers for convenience.

Usage::

    # Define an event
    from dataclasses import dataclass
    from domains.shared.events import DomainEvent, subscribe, publish

    @dataclass(frozen=True)
    class UserSignedUp(DomainEvent):
        user_id: str
        email: str

    # Register a handler (e.g. in your domain service)
    @subscribe(UserSignedUp)
    async def _on_signup(event: UserSignedUp) -> None:
        # send welcome email, create default resources, etc.
        ...

    # Publish from service layer
    await publish(UserSignedUp(user_id=str(user.id), email=user.email))

Notes
-----
* All handlers are ``async``; the bus awaits them concurrently via
  ``asyncio.gather``.
* Errors in one handler do NOT cancel other handlers.
* The bus is intentionally simple — no persistence, no replay, no ordering
  guarantees.  Upgrade to an external broker (Redis Streams, RabbitMQ, etc.)
  when you need durability.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Type alias for an async event handler function
EventHandler = Callable[..., Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# DomainEvent — base data-carrier
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Base class for all domain events.

    Subclass and add fields that describe the specific domain fact.  Events
    are immutable (``frozen=True``) by default — all fields must be set at
    construction time.

    The base class uses ``kw_only=True`` so its auto-generated fields
    (``event_id``, ``occurred_at``) remain keyword-only.  This lets subclasses
    freely define required positional fields without hitting the
    "non-default argument follows default argument" ``TypeError``::

        @dataclass(frozen=True)
        class UserVerified(DomainEvent):
            user_id: str   # required, positional — works because base is kw_only

        evt = UserVerified(user_id="abc-123")
        print(evt.event_type)     # "UserVerified"
        print(evt.event_id)       # <UUID>

    Attributes
    ----------
    event_id:
        Globally unique event identifier (auto-generated UUID).
    event_type:
        Fully qualified event class name — set automatically.
    occurred_at:
        UTC timestamp of when the event was created (not persisted).
    """

    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def event_type(self) -> str:
        """Unqualified class name — used as the dispatch key."""
        return self.__class__.__name__


# ---------------------------------------------------------------------------
# DomainEventBus — in-process fanout bus
# ---------------------------------------------------------------------------


class DomainEventBus:
    """Lightweight, in-process domain event bus.

    * Thread-safe handler registration (dict is GIL-protected for simple ops).
    * Concurrent dispatch via ``asyncio.gather``.
    * Errors in individual handlers are logged but do not propagate.

    This is a process-singleton; obtain the shared instance via
    :data:`event_bus` or use the module-level :func:`publish` /
    :func:`subscribe` helpers.
    """

    def __init__(self) -> None:
        # { EventClass: [handler, handler, ...] }
        self._handlers: dict[type[DomainEvent], list[EventHandler]] = defaultdict(list)

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: EventHandler,
    ) -> None:
        """Register *handler* to receive events of *event_type*.

        Parameters
        ----------
        event_type:
            The :class:`DomainEvent` subclass to listen for.
        handler:
            An async callable ``async def handler(event: <EventType>) -> None``.
        """
        self._handlers[event_type].append(handler)
        logger.debug(
            "domain_event_subscribed",
            event_type=event_type.__name__,
            handler=f"{handler.__module__}.{handler.__qualname__}",
        )

    async def publish(self, event: DomainEvent) -> None:
        """Dispatch *event* to all registered handlers concurrently.

        Errors in individual handlers are caught, logged, and ignored — they
        do NOT prevent other handlers from running.

        Parameters
        ----------
        event:
            The domain event to dispatch.
        """
        handlers = self._handlers.get(type(event), [])
        if not handlers:
            logger.debug(
                "domain_event_no_handlers",
                event_type=event.event_type,
                event_id=str(event.event_id),
            )
            return

        logger.debug(
            "domain_event_dispatching",
            event_type=event.event_type,
            event_id=str(event.event_id),
            handler_count=len(handlers),
        )

        results = await asyncio.gather(
            *(h(event) for h in handlers),
            return_exceptions=True,
        )

        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "domain_event_handler_error",
                    event_type=event.event_type,
                    event_id=str(event.event_id),
                    handler=f"{handler.__module__}.{handler.__qualname__}",
                    error=str(result),
                )

    def clear(self) -> None:
        """Remove all registered handlers (useful in tests)."""
        self._handlers.clear()


# ---------------------------------------------------------------------------
# Module-level singleton + convenience helpers
# ---------------------------------------------------------------------------

#: Shared in-process event bus.  Import and use directly.
event_bus: DomainEventBus = DomainEventBus()


async def publish(event: DomainEvent) -> None:
    """Publish *event* to the shared :data:`event_bus`."""
    await event_bus.publish(event)


def subscribe(event_type: type[DomainEvent]) -> Callable[[EventHandler], EventHandler]:
    """Decorator — register the decorated function as an event handler.

    Usage::

        @subscribe(UserSignedUp)
        async def _send_welcome_email(event: UserSignedUp) -> None:
            ...
    """

    def _decorator(func: EventHandler) -> EventHandler:
        event_bus.subscribe(event_type, func)
        return func

    return _decorator
