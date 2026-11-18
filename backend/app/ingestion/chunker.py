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


# Bumped whenever chunk boundaries or text change; stored on every chunk so `erag reindex` can find stale ones.
CHUNKER_VERSION = 2

_tokenizer: Any = None
_tokenizer_checked = False


def _load_tokenizer() -> Any:
    """The bge-m3 tokenizer when installed (`erag models install bge-m3-tokenizer`), else None."""
    global _tokenizer, _tokenizer_checked
    if not _tokenizer_checked:
        _tokenizer_checked = True
        from app.ml.models import installed, model_path

        if installed("bge-m3-tokenizer"):
            from tokenizers import Tokenizer

            _tokenizer = Tokenizer.from_file(str(model_path("bge-m3-tokenizer") / "tokenizer.json"))
    return _tokenizer


def estimate_tokens(text: str) -> int:
    """Exact bge-m3 token count when the tokenizer is installed, else about 4 characters per token."""
    tok = _load_tokenizer()
    if tok is not None:
        return max(1, len(tok.encode(text, add_special_tokens=False).ids))
    return max(1, len(text) // 4)


def _is_table(paragraph: str) -> bool:
    lines = [ln for ln in paragraph.splitlines() if ln.strip()]
    return len(lines) >= 3 and sum("|" in ln for ln in lines) >= 0.8 * len(lines)


def _split_table(paragraph: str, max_chars: int) -> list[str]:
    """Split a long table by rows, repeating the header (and a Markdown separator row) in every piece."""
    lines = [ln for ln in paragraph.splitlines() if ln.strip()]
    head = lines[:2] if len(lines) > 1 and set(lines[1].replace("|", "").strip()) <= set("-: ") else lines[:1]
    body = lines[len(head) :]
    pieces, cur = [], list(head)
    for row in body:
        if len("\n".join([*cur, row])) > max_chars and len(cur) > len(head):
            pieces.append("\n".join(cur))
            cur = [*head]
        cur.append(row)
    if len(cur) > len(head):
        pieces.append("\n".join(cur))
    return pieces


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
        paragraphs: list[tuple[str, bool]] = []  # (text, is a piece of a split table)
        for p in re.split(r"\n\s*\n", section.text):
            p = p.strip()
            if not p:
                continue
            if _is_table(p) and len(p) > max_chars:
                # A long table is split by rows with the header repeated; each piece is a chunk of its own.
                paragraphs.extend((piece, True) for piece in _split_table(p, max_chars))
            else:
                # Short tables are ordinary paragraphs (whole, never cut).
                paragraphs.extend((piece, False) for piece in ([p] if _is_table(p) else _split_long(p, max_chars)))
        buf = ""

        def emit(text: str, meta: dict[str, Any] = section.meta) -> None:
            drafts.append(ChunkDraft(len(drafts), text, estimate_tokens(text), dict(meta)))

        for p, table_piece in paragraphs:
            if table_piece:
                if buf.strip():
                    emit(buf)
                emit(p)
                buf = ""
            elif buf and len(buf) + len(p) + 2 > max_chars:
                emit(buf)
                tail = buf[-overlap_chars:]
                cut = tail.find(" ")
                buf = (tail[cut + 1 :] if cut >= 0 else tail) + "\n\n" + p
            else:
                buf = f"{buf}\n\n{p}" if buf else p
        if buf.strip():
            emit(buf)
    return drafts
