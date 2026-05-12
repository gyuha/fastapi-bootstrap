"""Shared domain kernel — cross-cutting primitives for the modular monolith.

This package provides the foundational abstractions that other bounded contexts
(auth, chat, …) can import WITHOUT creating cyclic inter-domain dependencies.

Exports
-------
- :class:`Entity`         — domain object with UUID identity
- :class:`AggregateRoot`  — lifecycle-owning top-level entity
- :class:`ValueObject`    — immutable, equality-by-value concept
- :class:`DomainEvent`    — base class for cross-domain events
- Type aliases            — ``UserId``, ``ConversationId``, ``PermissionKey``

Domain-isolation contract
--------------------------
* ``shared`` may NOT import from ``auth`` or ``chat``.
* ``auth`` and ``chat`` MAY import from ``shared``.
* ``shared`` only imports from ``app.core`` (infrastructure) or the
  Python standard library.
"""

from domains.shared.base import AggregateRoot, Entity, ValueObject
from domains.shared.events import DomainEvent, DomainEventBus
from domains.shared.types import (
    ConversationId,
    MessageId,
    PermissionKey,
    UserId,
)

__all__ = [
    "AggregateRoot",
    "ConversationId",
    "DomainEvent",
    "DomainEventBus",
    "Entity",
    "MessageId",
    "PermissionKey",
    "UserId",
    "ValueObject",
]
