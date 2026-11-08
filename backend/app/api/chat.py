"""Chat (SSE streaming), conversations and models."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth
from app.core.config import get_settings
from app.core.db import get_db
from app.core.ratelimit import RateLimiter
from app.ingestion.embedder import get_embedder
from app.llm.router import get_router
from app.models import Classification, Conversation, Message
from app.orchestrator.chat import ChatOrchestrator, ConversationNotFound, ToolRuntime, sse

router = APIRouter(prefix="/api", tags=["chat"])
chat_limiter = RateLimiter(get_settings().chat_rate_limit_per_minute)


def get_tool_runtime() -> ToolRuntime | None:
    return None  # MCP tools arrive in Phase 5


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None
    model: str | None = None


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    citations: dict[str, Any]
    model_used: str | None
    classification: Classification
    created_at: datetime


class ModelOut(BaseModel):
    id: str
    provider: str
    model: str
    locality: str
    max_classification: Classification


@router.post("/chat")
async def chat(
    body: ChatIn, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> StreamingResponse:
    if not chat_limiter.allow(str(auth.user.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many chat requests, slow down")
    if body.conversation_id is not None:
        conv = await db.get(Conversation, body.conversation_id)
        if conv is None or conv.user_id != auth.user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    orch = ChatOrchestrator(db, get_embedder(), get_router(), get_tool_runtime())

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in orch.run(auth.user, auth.perms, body.message, body.conversation_id, body.model):
                yield sse(event)
        except ConversationNotFound:
            yield sse({"type": "error", "message": "Conversation not found"})
        except Exception as exc:
            await db.rollback()
            yield sse({"type": "error", "message": f"Internal error: {type(exc).__name__}"})
            raise

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get("/conversations")
async def list_conversations(
    auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> list[ConversationOut]:
    rows = (
        await db.scalars(
            select(Conversation).where(Conversation.user_id == auth.user.id).order_by(Conversation.created_at.desc())
        )
    ).all()
    return [ConversationOut(id=c.id, title=c.title, created_at=c.created_at) for c in rows]


async def _own(db: AsyncSession, auth: AuthContext, conversation_id: uuid.UUID) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != auth.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conv


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: uuid.UUID, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> list[MessageOut]:
    conv = await _own(db, auth, conversation_id)
    rows = (
        await db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at))
    ).all()
    return [
        MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            citations=m.citations,
            model_used=m.model_used,
            classification=m.classification,
            created_at=m.created_at,
        )
        for m in rows
    ]


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> None:
    conv = await _own(db, auth, conversation_id)
    await db.delete(conv)
    await db.commit()


@router.get("/models")
async def list_models(_: AuthContext = Depends(get_auth)) -> list[ModelOut]:
    return [
        ModelOut(
            id=m.id, provider=m.provider, model=m.model, locality=m.locality, max_classification=m.max_classification
        )
        for m in get_router().models()
    ]
