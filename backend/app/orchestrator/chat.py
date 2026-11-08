"""Request state machine (PLAN.md section 6).

resolve user -> effective permissions -> authorized retrieval -> history re-validation ->
visible tools -> route on effective classification -> prompt -> generate (+ tool loop with
re-routing) -> persist with citations and model -> audit.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, readable_document_ids
from app.core.config import Settings, get_settings
from app.ingestion.embedder import Embedder
from app.llm.prompt_builder import NO_INFO_ANSWER, HistoryTurn, build_messages, render_tool_result
from app.llm.providers.base import Done, LLMProvider, ProviderError, TextDelta, ToolCall, ToolCallsEvent, ToolSpec
from app.llm.router import ModelRouter, RoutingRefused
from app.models import Classification, Conversation, Message, User
from app.rag.context_builder import BuiltContext, build_context
from app.rag.retrieval import RetrievalResult, Retriever
from app.services.audit import audit
from app.services.spend import cost_from_usage, ensure_budget, record_spend

log = logging.getLogger(__name__)

WITHHELD = "[Earlier answer withheld: its sources are no longer accessible to you.]"


class ConversationNotFound(Exception):
    pass


@dataclass
class ToolOutcome:
    """What the tool runtime returns for one call (already authorized and validated)."""

    content: str
    classification: Classification
    ok: bool
    summary: dict[str, Any] = field(default_factory=dict)


class ToolRuntime(Protocol):
    """Implemented by the MCP layer (app.mcp.tool_loop)."""

    async def visible_tools(self, db: AsyncSession, perms: EffectivePermissions) -> list[ToolSpec]: ...

    async def execute(
        self, db: AsyncSession, user: User, perms: EffectivePermissions, call: ToolCall
    ) -> ToolOutcome: ...


@dataclass
class PreparedChat:
    conversation: Conversation
    question: str
    retrieval: RetrievalResult
    context: BuiltContext
    history: list[HistoryTurn]
    history_classification: Classification
    withheld_message_ids: list[uuid.UUID]
    tools: list[ToolSpec]
    classification: Classification
    provider: LLMProvider | None
    refusal: str | None
    messages: list[dict[str, Any]]


class ChatOrchestrator:
    def __init__(
        self,
        db: AsyncSession,
        embedder: Embedder,
        router: ModelRouter,
        tool_runtime: ToolRuntime | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.router = router
        self.tools = tool_runtime
        self.settings = settings or get_settings()

    # --- preparation (no LLM involved) ---------------------------------------------------------

    async def _conversation(self, user: User, conversation_id: uuid.UUID | None, question: str) -> Conversation:
        if conversation_id is not None:
            conv = await self.db.get(Conversation, conversation_id)
            if conv is None or conv.user_id != user.id:
                raise ConversationNotFound()
            return conv
        conv = Conversation(user_id=user.id, title=question[:80] or "New conversation")
        self.db.add(conv)
        await self.db.flush()
        return conv

    async def _history(
        self, perms: EffectivePermissions, conv: Conversation
    ) -> tuple[list[HistoryTurn], Classification, list[uuid.UUID]]:
        """Last N turns, with answers whose cited documents are no longer readable withheld."""
        rows = list(
            (
                await self.db.scalars(
                    select(Message)
                    .where(Message.conversation_id == conv.id)
                    .order_by(Message.created_at.desc())
                    .limit(self.settings.history_max_messages)
                )
            ).all()
        )[::-1]
        cited: set[uuid.UUID] = set()
        for m in rows:
            for c in m.citations.get("items", []):
                cited.add(uuid.UUID(c["document_id"]))
        readable = await readable_document_ids(self.db, perms, cited)
        turns: list[HistoryTurn] = []
        withheld: list[uuid.UUID] = []
        classes: list[Classification] = []
        for m in rows:
            docs = {uuid.UUID(c["document_id"]) for c in m.citations.get("items", [])}
            tool_class = m.citations.get("tool_classification")
            if m.role == "assistant" and (not docs <= readable or m.citations.get("withhold")):
                withheld.append(m.id)
                turns.append(HistoryTurn("assistant", WITHHELD))
                continue
            classes.append(m.classification)
            if tool_class:
                classes.append(Classification(tool_class))
            turns.append(HistoryTurn(m.role, m.content))
        return turns, Classification.max(*classes), withheld

    async def prepare(
        self,
        user: User,
        perms: EffectivePermissions,
        question: str,
        conversation_id: uuid.UUID | None = None,
        requested_model: str | None = None,
    ) -> PreparedChat:
        conv = await self._conversation(user, conversation_id, question)
        retriever = Retriever(self.db, self.embedder)
        retrieval = await retriever.retrieve(
            perms, question, self.settings.retrieval_top_k, self.settings.retrieval_min_score
        )
        context = build_context(retrieval.hits, self.settings.context_token_budget)
        history, history_cls, withheld = await self._history(perms, conv)
        tools = await self.tools.visible_tools(self.db, perms) if self.tools else []

        classification = Classification.max(context.classification, history_cls)
        provider: LLMProvider | None = None
        refusal: str | None = None
        if context.items or tools:
            try:
                provider = self.router.choose(classification, requested_model)
            except RoutingRefused as exc:
                refusal = exc.reason
        messages = build_messages(question, context, history, tools_available=bool(tools))
        return PreparedChat(
            conversation=conv,
            question=question,
            retrieval=retrieval,
            context=context,
            history=history,
            history_classification=history_cls,
            withheld_message_ids=withheld,
            tools=tools,
            classification=classification,
            provider=provider,
            refusal=refusal,
            messages=messages,
        )

    # --- generation ----------------------------------------------------------------------------

    async def run(
        self,
        user: User,
        perms: EffectivePermissions,
        question: str,
        conversation_id: uuid.UUID | None = None,
        requested_model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        prep = await self.prepare(user, perms, question, conversation_id, requested_model)
        conv = prep.conversation
        self.db.add(
            Message(conversation_id=conv.id, role="user", content=question, classification=Classification.public)
        )
        await self.db.commit()

        citations = [i.citation() for i in prep.context.items]
        yield {"type": "meta", "conversation_id": str(conv.id)}
        yield {"type": "citations", "items": citations}

        classification = prep.classification
        provider = prep.provider
        answer = ""
        tool_log: list[dict[str, Any]] = []
        models_used: list[str] = []
        refusal = prep.refusal

        if refusal is None and provider is None:
            # Nothing authorized to ground an answer on, and no tools: do not ask a model at all.
            answer = NO_INFO_ANSWER
            yield {"type": "delta", "text": answer}
        elif refusal is None and provider is not None:
            yield {"type": "route", "model": provider.model_id, "classification": classification.value}
            messages = list(prep.messages)
            max_iter = self.settings.tool_max_iterations
            try:
                for iteration in range(max_iter + 1):
                    offer_tools = prep.tools if (self.tools and iteration < max_iter) else None
                    calls: list[ToolCall] = []
                    step_text = ""
                    if provider.locality == "cloud":
                        # Hard monthly ceiling, checked before anything leaves the process (ADR 0016).
                        await ensure_budget(self.db, provider.provider, self.settings.openrouter_monthly_budget_usd)
                    await self._audit_egress(user, provider, classification, prep, iteration)
                    models_used.append(provider.model_id)
                    usage: dict[str, Any] = {}
                    async for ev in provider.stream_chat(messages, tools=offer_tools, classification=classification):
                        if isinstance(ev, TextDelta):
                            step_text += ev.text
                            yield {"type": "delta", "text": ev.text}
                        elif isinstance(ev, ToolCallsEvent):
                            calls = ev.calls
                        elif isinstance(ev, Done):
                            usage = ev.usage
                    if provider.locality == "cloud":
                        await record_spend(self.db, provider.provider, cost_from_usage(usage))
                    answer += step_text
                    if not calls or self.tools is None:
                        break
                    messages.append(
                        {
                            "role": "assistant",
                            "content": step_text,
                            "tool_calls": [
                                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                                for c in calls
                            ],
                        }
                    )
                    for call in calls:
                        yield {"type": "tool_call", "id": call.id, "name": call.name, "arguments": call.arguments}
                        outcome = await self.tools.execute(self.db, user, perms, call)
                        await self.db.commit()
                        tool_log.append({"name": call.name, "ok": outcome.ok, **outcome.summary})
                        yield {
                            "type": "tool_result",
                            "id": call.id,
                            "name": call.name,
                            "ok": outcome.ok,
                            "classification": outcome.classification.value,
                        }
                        classification = Classification.max(classification, outcome.classification)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": render_tool_result(call.name, outcome.content, outcome.classification.value),
                            }
                        )
                    # Tool results may raise the classification: re-route before anything is sent.
                    new_provider = reroute(self.router, provider, classification)
                    if new_provider is not provider:
                        provider = new_provider
                        yield {"type": "route", "model": provider.model_id, "classification": classification.value}
            except RoutingRefused as exc:
                refusal = exc.reason
            except ProviderError as exc:
                log.warning("provider error", extra={"error": str(exc)})
                yield {"type": "error", "message": f"The model failed to answer: {exc}"}
                answer = answer or ""

        if refusal is not None:
            answer = refusal
            yield {"type": "refusal", "reason": refusal, "classification": classification.value}

        msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content=answer.strip(),
            citations={
                "items": citations,
                "tools": tool_log,
                "tool_classification": classification.value if tool_log else None,
            },
            model_used=models_used[-1] if models_used else None,
            classification=classification,
        )
        self.db.add(msg)
        audit(
            self.db,
            "chat.answer",
            user_id=user.id,
            resource=f"conversation:{conv.id}",
            decision="refuse" if refusal else "allow",
            details={
                "chunk_ids": [str(c) for c in prep.context.chunk_ids],
                "dropped_chunk_ids": [str(c) for c in prep.retrieval.dropped_chunk_ids],
                "withheld_messages": [str(m) for m in prep.withheld_message_ids],
                "classification": classification.value,
                "models": models_used,
                "tools_offered": [t.name for t in prep.tools],
                "tools_called": tool_log,
            },
        )
        await self.db.commit()
        yield {
            "type": "done",
            "message_id": str(msg.id),
            "model": msg.model_used,
            "classification": classification.value,
            "answer": msg.content,
        }

    async def _audit_egress(
        self, user: User, provider: LLMProvider, classification: Classification, prep: PreparedChat, iteration: int
    ) -> None:
        audit(
            self.db,
            "llm.request",
            user_id=user.id,
            resource=provider.model_id,
            details={
                "locality": provider.locality,
                "classification": classification.value,
                "iteration": iteration,
                "conversation_id": str(prep.conversation.id),
            },
        )
        await self.db.commit()


def reroute(router: ModelRouter, current: LLMProvider, classification: Classification) -> LLMProvider:
    """Keep the current model if it is cleared for the new classification, else pick one that is.

    Raises RoutingRefused when no model is cleared: the higher-classified data is then never sent.
    """
    if classification.rank <= current.max_classification.rank:
        return current
    return router.choose(classification)


def sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
