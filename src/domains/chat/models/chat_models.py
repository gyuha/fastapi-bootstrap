"""Chat domain SQLAlchemy ORM models.

Tables
------
* conversations — LLM conversation sessions (one per user/context)
* messages      — individual turns in a conversation

The ``title`` field is NULL on creation and auto-filled after the first
complete turn by a background LLM summarisation call in the service layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from core.database import Base


class Conversation(Base):
    """An LLM conversation session.

    Attributes
    ----------
    user_id:
        Foreign key to ``users.id``.
    title:
        Auto-generated title (first LLM summary).  ``None`` until the first
        turn completes.
    system_prompt:
        Optional system prompt prepended to every turn in this conversation.
    model_name:
        The LiteLLM model identifier active when the conversation was created,
        e.g. ``"openai/gpt-4o-mini"``.
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages: Mapped[list[Message]] = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    def __repr__(self) -> str:
        return f"<Conversation id={self.id!r} title={self.title!r}>"


class Message(Base):
    """A single message turn in a :class:`Conversation`.

    Attributes
    ----------
    role:
        ``"user"``, ``"assistant"``, or ``"system"``.
    content:
        The full message text.
    token_count:
        Number of tokens in *content* (approximate, model-dependent).
        ``None`` if not yet measured.
    finish_reason:
        The reason the model stopped generating: ``"stop"``, ``"length"``,
        ``"tool_calls"``, or ``None`` for user messages.
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship("Conversation", back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message id={self.id!r} role={self.role!r}>"
