"""Shared domain base classes.

Provides abstract base types for the DDD building blocks used across all
bounded contexts:

- :class:`Entity`        — has identity (UUID), equality by ``id``.
- :class:`AggregateRoot` — top-level entity that owns aggregate lifecycle.
- :class:`ValueObject`   — immutable; equality by value (all fields).

These are **pure Python dataclasses** — they do NOT inherit from SQLAlchemy's
:class:`~app.core.database.Base`.  ORM models live in each domain's
``models.py`` and extend ``Base`` directly.  These base classes add domain
semantics on top (DDD vocabulary) without leaking infrastructure concerns.

Usage::

    from domains.shared.base import Entity, ValueObject

    class UserId(ValueObject):
        value: uuid.UUID

    class UserAccount(Entity):
        email: str
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Entity — domain object with stable identity
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Entity:
    """Base class for domain entities.

    Entities have a stable, globally unique identity (UUID).  Two instances
    with the same ``id`` represent the same domain concept regardless of their
    other field values.

    Do **not** use this class as a SQLAlchemy mixin — ORM models inherit from
    :class:`~app.core.database.Base` directly.

    .. important:: **Subclasses MUST use** ``@dataclass(eq=False)`` **to preserve
       identity-based equality.**  Python's ``@dataclass(eq=True)`` (the default)
       generates a new ``__eq__`` on the subclass that compares *all* fields,
       silently overriding the ID-based ``__eq__`` defined here.  Using
       ``eq=False`` tells the dataclass machinery to leave ``__eq__`` and
       ``__hash__`` alone so they are inherited from :class:`Entity`.

    Example::

        @dataclass(eq=False)        # ← eq=False is required for identity semantics
        class UserAccount(Entity):
            email: str
            is_active: bool = True

        a = UserAccount(email="alice@example.com")
        b = UserAccount(id=a.id, email="alice@example.com")
        assert a == b   # same identity → equal
        assert hash(a) == hash(b)
    """

    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Entity):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id!r}>"


# ---------------------------------------------------------------------------
# AggregateRoot — lifecycle-owning top-level entity
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class AggregateRoot(Entity):
    """Aggregate root — the entry point and lifecycle owner for an aggregate.

    An *Aggregate Root* is the only object in a cluster of related entities
    that outside code is allowed to reference directly.  Sub-entities within
    the aggregate are accessed through the root.

    Adds standard timestamp fields (``created_at``, ``updated_at``) and a
    version counter for optimistic concurrency.

    Example::

        @dataclass
        class Conversation(AggregateRoot):
            title: str | None = None
            messages: list[Message] = field(default_factory=list)

            def add_message(self, role: str, content: str) -> None:
                self.messages.append(Message(role=role, content=content))
    """

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Optimistic-concurrency version counter.  Increment on every write.
    version: int = field(default=0)

    def touch(self) -> None:
        """Update ``updated_at`` to the current UTC timestamp."""
        self.updated_at = datetime.now(UTC)


# ---------------------------------------------------------------------------
# ValueObject — immutable, equality by value
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueObject:
    """Base class for domain value objects.

    Value objects have **no identity** — they are equal when all their fields
    are equal.  They are always immutable (``frozen=True``).  Use them to
    represent domain concepts like money amounts, addresses, coordinates, or
    validated string types (e.g. ``Email``, ``PermissionKey``).

    Example::

        @dataclass(frozen=True)
        class Email(ValueObject):
            value: str

            def __post_init__(self) -> None:
                if "@" not in self.value:
                    raise ValueError(f"Invalid email: {self.value!r}")

        e1 = Email("alice@example.com")
        e2 = Email("alice@example.com")
        assert e1 == e2   # equality by value
        assert hash(e1) == hash(e2)
    """
