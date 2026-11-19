"""Answer-level evaluation (R15, `erag eval run --with-answers`): generate the answer with a LOCAL chat model from
the retrieved context, check its citations against the expected documents, and ask the same local model whether
every statement is supported by the sources. Indicative only: model judgements are noisy and never gate CI.

Only a local provider may be used: evaluation sets and sources are confidential (ADR 0048).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from app.core.config import get_settings
from app.llm.prompt_builder import build_messages, render_context
from app.llm.providers.base import Done, LLMProvider, TextDelta
from app.models import Classification
from app.rag.context_builder import build_context
from app.rag.vector_store import VectorHit

CITE = re.compile(r"\[(\d{1,2})\]")
JUDGE = (
    "You check whether an answer is supported by its sources. Reply with exactly one word: YES if every factual "
    "statement in the ANSWER is stated in the SOURCES, NO otherwise."
)


async def _complete(provider: LLMProvider, messages: list[dict[str, Any]], max_tokens: int) -> str:
    out = []
    async for ev in provider.stream_chat(messages, classification=Classification.confidential, max_tokens=max_tokens):
        if isinstance(ev, TextDelta):
            out.append(ev.text)
        elif isinstance(ev, Done):
            break
    return "".join(out).strip()


async def answer_metrics(
    provider: LLMProvider, question: str, hits: list[VectorHit], keys: dict[uuid.UUID, str], expected: list[str]
) -> dict[str, float]:
    if provider.locality != "local":
        raise ValueError("answer evaluation only runs on a local model")
    ctx = build_context(hits, get_settings().context_token_budget)
    if not ctx.items:
        return {}
    answer = await _complete(provider, build_messages(question, ctx, []), 400)
    cited = {int(n) for n in CITE.findall(answer)}
    cited_docs = {keys[ctx.items[n - 1].document_id] for n in cited if 1 <= n <= len(ctx.items)}
    verdict = await _complete(
        provider,
        [
            {"role": "system", "content": JUDGE},
            {"role": "user", "content": f"SOURCES:\n{render_context(ctx)}\n\nANSWER:\n{answer}"},
        ],
        5,
    )
    return {
        "citation_precision": (len(cited_docs & set(expected)) / len(cited_docs)) if cited_docs else 0.0,
        "cited": float(len(cited_docs)),
        "faithful": 1.0 if verdict.upper().startswith("YES") else 0.0,
    }
