"""Chat domain HTTP router.

Provides REST + SSE endpoints for LLM-backed conversations.  The active LLM
provider is determined exclusively by the ``LLM_PROVIDER`` environment variable
— switching providers requires only an env-var change, no code modifications.

Routes
------
POST /chat/complete
    Single-turn (or multi-turn) non-streaming completion.  Returns the full
    LLM response in one JSON body.

POST /chat/stream
    Server-Sent Events (SSE) streaming completion.  Yields text fragments as
    they arrive from the LLM provider.  Each event carries one chunk; a final
    ``[DONE]`` sentinel is sent when the stream ends.

GET /chat/provider
    Returns the currently active LLM provider, model name, and full
    LiteLLM model identifier.  Useful for health checks and integration tests.

FastAPI dependency chain
------------------------
::

    Request
        → get_chat_service(factory=get_llm_factory())
            → ChatService(llm_client=factory.get_llm_client())
                → ChatService.complete() / ChatService.stream()
                    → AbstractLLMPort.invoke() / .stream()

Provider switching is transparent because the dependency chain reads from
:func:`get_settings` on every request — changing ``LLM_PROVIDER`` in ``.env``
and restarting the server is all that is required.

Testing pattern — dependency override
--------------------------------------
Inject a stub factory to avoid real LLM calls::

    from domains.chat.container import get_llm_factory
    from domains.chat.ports import LLMClientProtocol

    class StubFactory:
        def get_llm_client(self) -> LLMClientProtocol:
            ...  # return your mock

    app.dependency_overrides[get_llm_factory] = lambda: StubFactory()
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from langchain_core.messages import AIMessage as LCAIMessage
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from core.config import get_settings
from core.database import get_async_session
from domains.auth.security import get_current_user, require_permission
from domains.chat.container import get_chat_service
from domains.chat.repository import ChatRepository
from domains.chat.schemas import (
    ConversationCreate,
    ConversationResponse,
    MessageResponse,
    SendMessageRequest,
)
from domains.chat.service import ChatService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """A single message in a conversation turn.

    Attributes
    ----------
    role:
        Speaker role — ``"user"``, ``"assistant"``, or ``"system"``.
        Unknown roles default to ``HumanMessage``.
    content:
        Text content of the message.
    """

    role: str
    content: str


class ChatRequest(BaseModel):
    """Request body for chat endpoints.

    Attributes
    ----------
    messages:
        Ordered conversation history.  Must contain at least one message.
    system:
        Optional system prompt prepended before all other messages.
        Equivalent to adding a ``{"role": "system", ...}`` entry at the front.
    """

    messages: list[ChatMessage]
    system: str | None = None


class ChatResponse(BaseModel):
    """Response body for the non-streaming ``/complete`` endpoint.

    Attributes
    ----------
    content:
        The model's full reply text.
    model:
        Active LiteLLM model identifier, e.g. ``"openai/gpt-4o-mini"``.
        Included for debugging and provider-routing verification.
    """

    content: str
    model: str | None = None


class ProviderInfoResponse(BaseModel):
    """Response body for the ``/provider`` info endpoint.

    Attributes
    ----------
    provider:
        Active provider name, e.g. ``"openai"``, ``"ollama"``.
    model:
        Base model name, e.g. ``"gpt-4o-mini"``, ``"llama3.2"``.
    litellm_model:
        Full LiteLLM model identifier, e.g. ``"openai/gpt-4o-mini"``.
    """

    provider: str
    model: str
    litellm_model: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_langchain_messages(body: ChatRequest) -> list[BaseMessage]:
    """Convert a :class:`ChatRequest` to a list of LangChain :class:`BaseMessage` objects.

    Conversion rules:

    * ``body.system`` → :class:`SystemMessage` prepended before all other messages.
    * ``role="system"``  → :class:`SystemMessage`
    * ``role="assistant"`` → :class:`AIMessage`
    * Any other role (incl. ``"user"``) → :class:`HumanMessage`

    Parameters
    ----------
    body:
        Incoming chat request.

    Returns
    -------
    list[BaseMessage]
        LangChain message list ready for :meth:`ChatService.complete` /
        :meth:`ChatService.stream`.
    """
    messages: list[BaseMessage] = []

    if body.system:
        messages.append(SystemMessage(content=body.system))

    for msg in body.messages:
        if msg.role == "system":
            messages.append(SystemMessage(content=msg.content))
        elif msg.role == "assistant":
            messages.append(LCAIMessage(content=msg.content))
        else:
            messages.append(HumanMessage(content=msg.content))

    return messages


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/complete",
    response_model=ChatResponse,
    summary="Non-streaming chat completion",
    description=(
        "Send a conversation to the active LLM provider and receive the full "
        "response in a single JSON body.  Provider is set via ``LLM_PROVIDER``."
    ),
)
async def chat_complete(
    body: ChatRequest,
    service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    """Non-streaming LLM chat completion.

    Converts the request to LangChain messages, delegates to the injected
    :class:`ChatService`, and wraps the response in :class:`ChatResponse`.

    Returns
    -------
    ChatResponse
        The model's complete reply with the active model identifier.

    Raises
    ------
    422
        If ``messages`` is an empty list.
    502
        If the underlying LLM provider returns an error.
    """
    if not body.messages:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one message is required",
        )

    lc_messages = _to_langchain_messages(body)

    try:
        result = await service.complete(lc_messages)
    except Exception as exc:
        logger.error("chat_complete_error", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM provider error: {exc!s}",
        ) from exc

    active_model = get_settings().llm.litellm_model
    logger.info(
        "chat_complete_success",
        model=active_model,
        content_length=len(str(result.content)),
    )
    return ChatResponse(
        content=str(result.content),
        model=active_model,
    )


@router.post(
    "/stream",
    summary="Streaming chat completion (SSE)",
    description=(
        "Stream the LLM response as Server-Sent Events.  Each ``data`` event "
        "carries a text chunk.  A final ``data: [DONE]`` event signals the end."
    ),
)
async def chat_stream(
    body: ChatRequest,
    service: ChatService = Depends(get_chat_service),
) -> EventSourceResponse:
    """SSE streaming LLM chat completion.

    Each non-empty text chunk from the provider is emitted as an SSE
    ``data`` event.  A sentinel ``[DONE]`` event is emitted when the stream
    ends.  Errors during streaming are emitted as ``event: error`` events
    so the client can detect failure without losing already-delivered tokens.

    Returns
    -------
    EventSourceResponse
        SSE stream with one ``data`` event per token chunk.

    Raises
    ------
    422
        If ``messages`` is an empty list.
    """
    if not body.messages:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one message is required",
        )

    lc_messages = _to_langchain_messages(body)

    active_model = get_settings().llm.litellm_model

    async def _event_generator() -> Any:
        """Yield SSE events from the LLM provider stream."""
        try:
            logger.info("chat_stream_start", model=active_model)
            async for chunk in service.stream(lc_messages):
                yield {"data": chunk}
            yield {"data": "[DONE]"}
            logger.info("chat_stream_complete", model=active_model)
        except Exception as exc:
            logger.error("chat_stream_error", error=str(exc), exc_info=True)
            yield {"event": "error", "data": str(exc)}

    return EventSourceResponse(_event_generator())


@router.get(
    "/provider",
    response_model=ProviderInfoResponse,
    summary="Active LLM provider info",
    description=(
        "Return the currently active LLM provider name, model, and full "
        "LiteLLM model identifier.  Useful for health checks and integration tests."
    ),
)
async def get_provider_info() -> ProviderInfoResponse:
    """Return the active LLM provider metadata.

    Reads from :func:`get_settings` so the response always reflects the
    current ``LLM_PROVIDER`` / ``LLM_DEFAULT_MODEL`` environment variables.
    No LLM call is made.

    Returns
    -------
    ProviderInfoResponse
        Active provider, model, and full LiteLLM model identifier.
    """
    s = get_settings()
    return ProviderInfoResponse(
        provider=s.llm.provider.value,
        model=s.llm.default_model,
        litellm_model=s.llm.litellm_model,
    )


# ---------------------------------------------------------------------------
# Conversation management endpoints (authenticated, DB-backed)
# ---------------------------------------------------------------------------


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new conversation",
    dependencies=[Depends(require_permission("chat:write"))],
)
async def create_conversation(
    body: ConversationCreate,
    current_user: Any = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> ConversationResponse:
    """Create a new conversation session for the authenticated user.

    Requires the ``chat:write`` permission.
    """
    repo = ChatRepository(session)
    active_model = get_settings().llm.litellm_model
    conv = await repo.create_conversation(
        user_id=current_user.id,
        title=body.title,
        system_prompt=body.system_prompt,
        model_name=active_model,
    )
    return ConversationResponse.model_validate(conv)


@router.get(
    "/conversations",
    response_model=list[ConversationResponse],
    summary="List conversations for authenticated user",
)
async def list_conversations(
    current_user: Any = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> list[ConversationResponse]:
    """Return all conversations for the authenticated user (newest first)."""
    repo = ChatRepository(session)
    convs = await repo.list_conversations(current_user.id)
    return [ConversationResponse.model_validate(c) for c in convs]


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    summary="Get a conversation",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: Any = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> ConversationResponse:
    """Return a conversation owned by the authenticated user."""
    repo = ChatRepository(session)
    conv = await repo.get_conversation(conversation_id, user_id=current_user.id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return ConversationResponse.model_validate(conv)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageResponse],
    summary="List messages in a conversation",
)
async def list_messages(
    conversation_id: uuid.UUID,
    current_user: Any = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> list[MessageResponse]:
    """Return all messages in a conversation (oldest first)."""
    repo = ChatRepository(session)
    conv = await repo.get_conversation(conversation_id, user_id=current_user.id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    msgs = await repo.get_conversation_messages(conversation_id)
    return [MessageResponse.model_validate(m) for m in msgs]


@router.post(
    "/conversations/{conversation_id}/messages",
    summary="Send a message — SSE streaming + DB persistence",
    dependencies=[Depends(require_permission("chat:write"))],
)
async def send_message(
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    current_user: Any = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
    service: ChatService = Depends(get_chat_service),
) -> Any:
    """Send a user message and receive an SSE-streamed assistant reply.

    Workflow
    --------
    1. Verify conversation ownership.
    2. Persist the user message.
    3. Rebuild LangChain message list from conversation history.
    4. Stream tokens from the LLM provider.
    5. Collect all chunks; persist the completed assistant message.
    6. If this is the first turn, auto-generate a title.

    Returns an :class:`~sse_starlette.sse.EventSourceResponse` when
    ``body.stream=True`` (default); returns a :class:`MessageResponse` JSON
    body when ``body.stream=False``.

    Requires the ``chat:write`` permission.
    """
    repo = ChatRepository(session)
    conv = await repo.get_conversation(conversation_id, user_id=current_user.id)
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        )

    # Persist user message
    await repo.add_message(conversation_id, "user", body.content)

    # Build message history
    lc_messages: list[BaseMessage] = []
    if conv.system_prompt:
        lc_messages.append(SystemMessage(content=conv.system_prompt))
    history = await repo.get_conversation_messages(conversation_id)
    for hist_msg in history:
        if hist_msg.role == "user":
            lc_messages.append(HumanMessage(content=hist_msg.content))
        elif hist_msg.role == "assistant":
            lc_messages.append(LCAIMessage(content=hist_msg.content))
        elif hist_msg.role == "system":
            lc_messages.append(SystemMessage(content=hist_msg.content))

    is_first_turn = len([m for m in history if m.role == "assistant"]) == 0

    if not body.stream:
        # Non-streaming path
        try:
            result = await service.complete(lc_messages)
        except Exception as exc:
            logger.error("chat_message_complete_error", error=str(exc), exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"LLM provider error: {exc!s}",
            ) from exc

        assistant_content = str(result.content)
        assistant_msg = await repo.add_message(
            conversation_id, "assistant", assistant_content, finish_reason="stop"
        )
        await session.commit()

        # Auto-title on first turn
        if is_first_turn and conv.title is None:
            await _auto_title(repo, conv.id, body.content, assistant_content, service, session)

        return MessageResponse.model_validate(assistant_msg)

    # SSE streaming path
    collected_chunks: list[str] = []

    async def _event_gen() -> Any:
        try:
            logger.info(
                "chat_stream_start",
                conversation_id=str(conversation_id),
                model=conv.model_name,
            )
            async for chunk in service.stream(lc_messages):
                collected_chunks.append(chunk)
                yield {"data": chunk}
            yield {"data": "[DONE]"}
            logger.info("chat_stream_complete", chunks=len(collected_chunks))
        except Exception as exc:
            logger.error("chat_stream_error", error=str(exc), exc_info=True)
            yield {"event": "error", "data": str(exc)}
        finally:
            # Persist assistant message after stream completes
            if collected_chunks:
                assistant_content = "".join(collected_chunks)
                try:
                    await repo.add_message(
                        conversation_id,
                        "assistant",
                        assistant_content,
                        finish_reason="stop",
                    )
                    await session.commit()
                    if is_first_turn and conv.title is None:
                        await _auto_title(
                            repo, conv.id, body.content, assistant_content, service, session
                        )
                except Exception as db_exc:
                    logger.error(
                        "chat_message_persist_failed",
                        error=str(db_exc),
                        exc_info=True,
                    )

    return EventSourceResponse(_event_gen())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _auto_title(
    repo: ChatRepository,
    conv_id: uuid.UUID,
    user_content: str,
    assistant_content: str,
    service: ChatService,
    session: AsyncSession,
) -> None:
    """Generate and save an auto-title after the first turn."""
    try:
        title_messages = [
            SystemMessage(
                content=(
                    "Generate a short, concise title (max 8 words) for this conversation. "
                    "Reply with only the title text, no quotes or punctuation."
                )
            ),
            HumanMessage(
                content=f"User: {user_content[:200]}\nAssistant: {assistant_content[:200]}"
            ),
        ]
        title_result = await service.complete(title_messages)
        title = str(title_result.content).strip()[:256]
        if title:
            await repo.update_conversation_title(conv_id, title)
            await session.commit()
            logger.info("conversation_title_set", conv_id=str(conv_id), title=title)
    except Exception as exc:
        logger.warning("auto_title_failed", conv_id=str(conv_id), error=str(exc))
