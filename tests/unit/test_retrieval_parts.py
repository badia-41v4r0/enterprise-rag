"""Fusion, retrieval settings, language detection, table-aware chunking and the in-process reranker."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.ingestion.chunker import _split_table, chunk_sections
from app.ingestion.parsers import Section
from app.ml.models import installed
from app.models import Classification
from app.rag.fusion import rrf
from app.rag.lexical import detect_language
from app.rag.settings import RetrievalSettings
from app.rag.vector_store import VectorHit


def _hit(name: str, score: float) -> VectorHit:
    return VectorHit(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
        document_id=uuid.uuid4(),
        container_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        ordinal=0,
        text=name,
        token_count=1,
        meta={},
        title=name,
        container_name="c",
        classification=Classification.public,
        restricted=False,
        score=score,
    )


def test_rrf_rewards_agreement_and_keeps_leg_scores() -> None:
    a, b, c = _hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)
    fused = rrf([a, b], [c, b], k=60)
    assert fused[0].hit.text == "b"  # found by both legs
    top = fused[0]
    assert top.vector_score == 0.8 and top.text_rank == 0.8 and top.legs == ["vector", "text"]
    assert top.fused == pytest.approx(1 / 62 + 1 / 62)
    only_text = rrf([], [a], text_weight=2.0)[0]
    assert only_text.vector_score is None and only_text.fused == pytest.approx(2 / 61)


def test_settings_validate_and_merge() -> None:
    s = RetrievalSettings().merged({"mode": "vector", "final_k": "6", "rerank_min_score": "0.1"})
    assert s.mode == "vector" and s.final_k == 6 and s.rerank_min_score == 0.1
    for bad in ({"mode": "x"}, {"final_k": 0}, {"reranker": "nope"}, {"bogus": 1}, {"text_weight": 11}):
        with pytest.raises(ValueError):
            RetrievalSettings().merged(bad)
    assert RetrievalSettings().merged({"reranker": ""}).reranker == ""


def test_language_detection() -> None:
    assert detect_language("El plazo de pago es de 60 días desde la fecha de cada factura del contrato.") == "spanish"
    assert detect_language("The payment term of the contract is 60 days from the date of the invoice.") == "english"
    assert detect_language("AX-1088 120 g IP67") == "simple"


def test_long_tables_split_by_rows_with_the_header_repeated() -> None:
    header = "| Product | Weight |\n|---|---|"
    rows = [f"| AX-{1000 + i} | {100 + i} g |" for i in range(80)]
    table = header + "\n" + "\n".join(rows)
    pieces = _split_table(table, 300)
    assert len(pieces) > 1
    assert all(p.startswith(header) and len(p) <= 300 for p in pieces)
    assert sum(p.count("| AX-") for p in pieces) == 80  # no row lost or cut
    drafts = chunk_sections([Section("Intro paragraph.\n\n" + table, {})], max_chars=300)
    assert any(d.text.startswith(header) for d in drafts)
    short = header + "\n" + "\n".join(rows[:3])
    assert [d.text for d in chunk_sections([Section(short, {})])] == [short]  # a short table stays whole


@pytest.mark.skipif(not installed("mmarco-minilm-l12"), reason="erag models install mmarco-minilm-l12")
def test_real_reranker_scores_and_times_out_into_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings
    from app.rag.reranker import RerankerUnavailable, rerank

    q = "What is the payment term of contract CT-2025-0137?"
    passages = ["Contract CT-2025-0137: the payment term is 60 days.", "The AX-1088 sensor weighs 120 g."]
    scores = asyncio.run(rerank("mmarco-minilm-l12", q, passages))
    assert scores[0] > 0.5 > scores[1]
    monkeypatch.setenv("ERAG_RERANKER_TIMEOUT_SECONDS", "0.000001")
    get_settings.cache_clear()
    try:
        with pytest.raises(RerankerUnavailable, match="did not answer"):
            asyncio.run(rerank("mmarco-minilm-l12", q, passages * 50))
    finally:
        get_settings.cache_clear()
