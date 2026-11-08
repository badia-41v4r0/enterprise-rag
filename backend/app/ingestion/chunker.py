"""Paragraph-aware chunker with overlap. Token counts are estimated (~4 chars per token)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.parsers import Section


@dataclass
class ChunkDraft:
    ordinal: int
    text: str
    token_count: int
    meta: dict[str, Any] = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    out, buf = [], ""
    for s in sentences:
        while len(s) > max_chars:  # pathological: no sentence breaks
            out.append(s[:max_chars])
            s = s[max_chars:]
        if len(buf) + len(s) + 1 > max_chars and buf:
            out.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        out.append(buf)
    return out


def chunk_sections(sections: list[Section], max_chars: int = 1200, overlap_chars: int = 150) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    for section in sections:
        paragraphs: list[str] = []
        for p in re.split(r"\n\s*\n", section.text):
            p = p.strip()
            if p:
                paragraphs.extend(_split_long(p, max_chars))
        buf = ""
        for p in paragraphs:
            if buf and len(buf) + len(p) + 2 > max_chars:
                drafts.append(ChunkDraft(len(drafts), buf, estimate_tokens(buf), dict(section.meta)))
                tail = buf[-overlap_chars:]
                cut = tail.find(" ")
                buf = (tail[cut + 1 :] if cut >= 0 else tail) + "\n\n" + p
            else:
                buf = f"{buf}\n\n{p}" if buf else p
        if buf.strip():
            drafts.append(ChunkDraft(len(drafts), buf, estimate_tokens(buf), dict(section.meta)))
    return drafts
