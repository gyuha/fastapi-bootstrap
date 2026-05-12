"""Unit tests for the shared domain kernel.

Tests cover:
- Entity equality and hashing by ID
- AggregateRoot touch() updates updated_at
- ValueObject immutability and equality by value
- DomainEventBus subscribe / publish / error isolation
- Type aliases are constructable from uuid.UUID
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from domains.shared.base import AggregateRoot, Entity, ValueObject
from domains.shared.events import DomainEvent, DomainEventBus
from domains.shared.types import (
    ConversationId,
    MessageId,
    PermissionKey,
    UserId,
)

# ---------------------------------------------------------------------------
# Entity tests
# ---------------------------------------------------------------------------


class TestEntity:
    def test_auto_generates_uuid(self) -> None:
        @dataclass(eq=False)
        class Thing(Entity):
            name: str = ""

        t = Thing()
        assert isinstance(t.id, uuid.UUID)

    def test_equality_by_id(self) -> None:
        # Entity subclasses MUST use eq=False so Python's @dataclass machinery
        # does not generate a field-based __eq__ that shadows Entity.__eq__.
        @dataclass(eq=False)
        class Thing(Entity):
            name: str = ""

        shared_id = uuid.uuid4()
        a = Thing(id=shared_id, name="alpha")
        b = Thing(id=shared_id, name="beta")
        assert a == b, "Same id → equal regardless of other fields"

    def test_inequality_different_id(self) -> None:
        @dataclass(eq=False)
        class Thing(Entity):
            pass

        assert Thing() != Thing(), "Different auto-generated IDs → not equal"

    def test_hashable_by_id(self) -> None:
        # eq=False preserves Entity.__hash__ so the subclass remains hashable.
        @dataclass(eq=False)
        class Thing(Entity):
            pass

        t = Thing()
        s: set[Thing] = {t}
        assert t in s

    def test_repr_contains_class_name_and_id(self) -> None:
        @dataclass(eq=False)
        class MyThing(Entity):
            pass

        t = MyThing()
        r = repr(t)
        assert "MyThing" in r
        assert str(t.id) in r


# ---------------------------------------------------------------------------
# AggregateRoot tests
# ---------------------------------------------------------------------------


class TestAggregateRoot:
    def test_inherits_entity(self) -> None:
        @dataclass(eq=False)
        class MyRoot(AggregateRoot):
            pass

        root = MyRoot()
        assert isinstance(root, Entity)

    def test_default_timestamps_are_utc(self) -> None:
        @dataclass(eq=False)
        class MyRoot(AggregateRoot):
            pass

        root = MyRoot()
        assert root.created_at.tzinfo is not None
        assert root.updated_at.tzinfo is not None

    def test_touch_updates_updated_at(self) -> None:
        @dataclass(eq=False)
        class MyRoot(AggregateRoot):
            pass

        root = MyRoot()
        original = root.updated_at
        # Simulate a tick
        root.touch()
        assert root.updated_at >= original

    def test_initial_version_is_zero(self) -> None:
        @dataclass(eq=False)
        class MyRoot(AggregateRoot):
            pass

        assert MyRoot().version == 0


# ---------------------------------------------------------------------------
# ValueObject tests
# ---------------------------------------------------------------------------


class TestValueObject:
    def test_equality_by_value(self) -> None:
        @dataclass(frozen=True)
        class Money(ValueObject):
            amount: int
            currency: str

        assert Money(100, "USD") == Money(100, "USD")
        assert Money(100, "USD") != Money(200, "USD")

    def test_immutable(self) -> None:
        @dataclass(frozen=True)
        class Tag(ValueObject):
            label: str

        t = Tag("python")
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            t.label = "java"  # type: ignore[misc]

    def test_hashable(self) -> None:
        @dataclass(frozen=True)
        class Color(ValueObject):
            r: int
            g: int
            b: int

        white = Color(255, 255, 255)
        palette: set[Color] = {white}
        assert Color(255, 255, 255) in palette


# ---------------------------------------------------------------------------
# DomainEvent tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserSignedUp(DomainEvent):
    user_id: str
    email: str


class TestDomainEvent:
    def test_event_type_is_class_name(self) -> None:
        evt = UserSignedUp(user_id="u1", email="a@example.com")
        assert evt.event_type == "UserSignedUp"

    def test_auto_event_id(self) -> None:
        evt = UserSignedUp(user_id="u1", email="a@example.com")
        assert isinstance(evt.event_id, uuid.UUID)

    def test_occurred_at_is_utc(self) -> None:
        evt = UserSignedUp(user_id="u1", email="a@example.com")
        assert evt.occurred_at.tzinfo is not None


# ---------------------------------------------------------------------------
# DomainEventBus tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDomainEventBus:
    async def test_handler_receives_event(self) -> None:
        bus = DomainEventBus()
        received: list[UserSignedUp] = []

        async def handler(event: UserSignedUp) -> None:
            received.append(event)

        bus.subscribe(UserSignedUp, handler)
        evt = UserSignedUp(user_id="u1", email="a@example.com")
        await bus.publish(evt)

        assert len(received) == 1
        assert received[0] is evt

    async def test_multiple_handlers_all_called(self) -> None:
        bus = DomainEventBus()
        results: list[str] = []

        async def h1(event: UserSignedUp) -> None:
            results.append("h1")

        async def h2(event: UserSignedUp) -> None:
            results.append("h2")

        bus.subscribe(UserSignedUp, h1)
        bus.subscribe(UserSignedUp, h2)
        await bus.publish(UserSignedUp(user_id="u", email="b@example.com"))

        assert set(results) == {"h1", "h2"}

    async def test_unrelated_event_not_dispatched(self) -> None:
        @dataclass(frozen=True)
        class OtherEvent(DomainEvent):
            pass

        bus = DomainEventBus()
        received: list[DomainEvent] = []

        async def handler(event: UserSignedUp) -> None:
            received.append(event)

        bus.subscribe(UserSignedUp, handler)
        await bus.publish(OtherEvent())

        assert received == []

    async def test_handler_error_does_not_cancel_others(self) -> None:
        bus = DomainEventBus()
        results: list[str] = []

        async def failing_handler(event: UserSignedUp) -> None:
            raise RuntimeError("intentional failure")

        async def good_handler(event: UserSignedUp) -> None:
            results.append("ok")

        bus.subscribe(UserSignedUp, failing_handler)
        bus.subscribe(UserSignedUp, good_handler)

        # Should NOT raise; good_handler should still run
        await bus.publish(UserSignedUp(user_id="u", email="c@example.com"))
        assert results == ["ok"]

    async def test_clear_removes_all_handlers(self) -> None:
        bus = DomainEventBus()
        calls: list[int] = []

        async def h(event: UserSignedUp) -> None:
            calls.append(1)

        bus.subscribe(UserSignedUp, h)
        bus.clear()
        await bus.publish(UserSignedUp(user_id="u", email="d@example.com"))
        assert calls == []


# ---------------------------------------------------------------------------
# Type alias tests
# ---------------------------------------------------------------------------


class TestTypeAliases:
    def test_user_id_is_uuid(self) -> None:
        raw = uuid.uuid4()
        uid = UserId(raw)
        assert uid == raw

    def test_conversation_id_is_uuid(self) -> None:
        raw = uuid.uuid4()
        cid = ConversationId(raw)
        assert cid == raw

    def test_message_id_is_uuid(self) -> None:
        raw = uuid.uuid4()
        mid = MessageId(raw)
        assert mid == raw

    def test_permission_key_is_str(self) -> None:
        pk = PermissionKey("chat:write")
        assert pk == "chat:write"
