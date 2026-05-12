"""Chat domain repository — all database I/O.

Usage::

    repo = ChatRepository(session)
    conv = await repo.create_conversation(user_id=uuid, system_prompt="You are...")
    msg  = await repo.add_message(conv.id, "user", "Hello!")
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from domains.chat.models import Conversation, Message


class ChatRepository:
    """Thin data-access layer for the chat domain."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Conversation ──────────────────────────────────────────────────────────

    async def create_conversation(
        self,
        user_id: uuid.UUID,
        title: str | None = None,
        system_prompt: str | None = None,
        model_name: str | None = None,
    ) -> Conversation:
        conv = Conversation(
            user_id=user_id,
            title=title,
            system_prompt=system_prompt,
            model_name=model_name,
        )
        self._session.add(conv)
        await self._session.flush()
        return conv

    async def get_conversation(
        self,
        conv_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> Conversation | None:
        """Fetch a conversation, optionally checking ownership."""
        stmt = (
            select(Conversation)
            .where(Conversation.id == conv_id)
            .options(selectinload(Conversation.messages))
        )
        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_conversations(self, user_id: uuid.UUID) -> Sequence[Conversation]:
        result = await self._session.execute(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
        )
        return result.scalars().all()

    async def update_conversation_title(self, conv_id: uuid.UUID, title: str) -> None:
        await self._session.execute(
            update(Conversation).where(Conversation.id == conv_id).values(title=title)
        )

    # ── Message ───────────────────────────────────────────────────────────────

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        token_count: int | None = None,
        finish_reason: str | None = None,
    ) -> Message:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            token_count=token_count,
            finish_reason=finish_reason,
        )
        self._session.add(msg)
        await self._session.flush()
        return msg

    async def get_conversation_messages(
        self,
        conv_id: uuid.UUID,
    ) -> Sequence[Message]:
        result = await self._session.execute(
            select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)
        )
        return result.scalars().all()
