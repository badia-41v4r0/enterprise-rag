"""Full-text leg of hybrid search (R14, ADR 0046): language detection, the chunk tsvector and the query.

Every chunk gets a tsvector made of
- the title, weighted A, in the document's language and without stemming (`simple`);
- the heading and text in the document's language (stemmed, stop words removed);
- the heading and text again with `simple`, so codes, numbers and names match exactly.
`unaccent` is applied to both sides, so "credito" finds "crédito".

A question becomes an OR query of its stemmed Spanish and English terms plus every token that contains a digit
(identifiers such as CT-2025-0137 or INV-2026-0125): natural questions are not "all of these words".
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ES = set(
    [
        "el",
        "la",
        "los",
        "las",
        "de",
        "del",
        "que",
        "y",
        "en",
        "un",
        "una",
        "por",
        "con",
        "para",
        "es",
        "se",
        "no",
        "al",
        "lo",
        "como",
        "más",
        "pero",
        "sus",
        "le",
        "ya",
        "o",
        "este",
        "esta",
        "cuál",
        "cuánto",
        "cuántos",
        "qué",
        "cómo",
        "dónde",
        "son",
        "está",
        "están",
        "hay",
        "entre",
        "sobre",
        "también",
        "fue",
        "ha",
        "muy",
        "desde",
        "todo",
        "nos",
        "durante",
        "uno",
        "ni",
        "contra",
        "ese",
        "eso",
        "mí",
        "qué",
        "otro",
        "esos",
        "esas",
        "estos",
        "estas",
        "su",
        "sus",
    ]
)
_EN = set(
    [
        "the",
        "of",
        "and",
        "to",
        "in",
        "a",
        "is",
        "that",
        "for",
        "it",
        "as",
        "with",
        "was",
        "on",
        "are",
        "be",
        "by",
        "this",
        "an",
        "at",
        "from",
        "or",
        "have",
        "which",
        "not",
        "but",
        "what",
        "all",
        "were",
        "when",
        "can",
        "there",
        "their",
        "how",
        "does",
        "do",
        "if",
        "will",
        "each",
        "about",
        "other",
        "many",
        "then",
        "them",
        "these",
        "so",
        "some",
        "would",
        "into",
        "has",
        "more",
        "her",
        "two",
        "like",
        "him",
        "see",
        "time",
        "could",
        "no",
        "make",
        "than",
        "first",
        "been",
        "its",
        "who",
        "now",
        "people",
        "my",
        "made",
        "over",
    ]
)
_WORD = re.compile(r"[a-záéíóúñü]+")

LANG_CONFIGS = ("spanish", "english", "simple")


def detect_language(sample: str) -> str:
    """`spanish`, `english` or `simple` (undecided), by stop-word counts on the first few thousand chars."""
    words = _WORD.findall(sample[:6000].lower())
    es = sum(w in _ES for w in words)
    en = sum(w in _EN for w in words)
    if max(es, en) < 3:
        return "simple"
    return "spanish" if es > en else "english"


TSV_UPDATE = text(
    """
    UPDATE chunks c SET
        lang = :lang,
        tsv = setweight(to_tsvector('simple', unaccent(d.title)), 'A')
           || setweight(to_tsvector(CAST(:cfg AS regconfig), unaccent(d.title)), 'A')
           || to_tsvector(CAST(:cfg AS regconfig), unaccent(coalesce(c.metadata->>'heading', '') || ' ' || c.text))
           || to_tsvector('simple', unaccent(coalesce(c.metadata->>'heading', '') || ' ' || c.text))
    FROM documents d
    WHERE d.id = c.document_id AND c.version_id = :version_id
    """
)


async def index_version_text(db: AsyncSession, version_id: uuid.UUID, lang: str) -> None:
    if lang not in LANG_CONFIGS:
        raise ValueError(f"unsupported text search configuration {lang}")
    await db.execute(TSV_UPDATE, {"lang": lang, "cfg": lang, "version_id": version_id})


# The OR query, built in SQL from the question. NULL when the question has no usable term.
# A word that is a stop word in either language is dropped before stemming: the Spanish configuration keeps
# "what", "is", "the", and the English one keeps "el", "de", "que", and in an OR query any of them matches
# nearly every chunk (a new hire asking about salaries got the handbook back as "relevant").
QUERY_SQL = """
    SELECT to_tsquery('simple', string_agg(DISTINCT quote_literal(lex), ' | '))
    FROM (
        SELECT unnest(tsvector_to_array(to_tsvector('spanish', w) || to_tsvector('english', w))) AS lex
        FROM unnest(tsvector_to_array(to_tsvector('simple', unaccent(:q)))) AS w
        WHERE numnode(plainto_tsquery('spanish', w)) > 0 AND numnode(plainto_tsquery('english', w)) > 0
        UNION
        SELECT w FROM unnest(tsvector_to_array(to_tsvector('simple', unaccent(:q)))) AS w WHERE w ~ '[0-9]'
    ) terms
"""
