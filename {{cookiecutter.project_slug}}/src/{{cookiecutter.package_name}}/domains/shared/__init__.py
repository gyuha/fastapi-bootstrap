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
* ``shared`` only imports from ``{{ cookiecutter.package_name }}.core`` (infrastructure) or the
  Python standard library.
"""

from {{ cookiecutter.package_name }}.domains.shared.base import AggregateRoot, Entity, ValueObject
from {{ cookiecutter.package_name }}.domains.shared.events import DomainEvent, DomainEventBus
from {{ cookiecutter.package_name }}.domains.shared.types import (
    ConversationId,
    MessageId,
    PermissionKey,
    UserId,
)

__all__ = [
    # Base classes
    "Entity",
    "AggregateRoot",
    "ValueObject",
    # Events
    "DomainEvent",
    "DomainEventBus",
    # Type aliases
    "UserId",
    "ConversationId",
    "MessageId",
    "PermissionKey",
]
