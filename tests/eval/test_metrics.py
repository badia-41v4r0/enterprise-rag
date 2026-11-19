"""Evaluation metrics on hand-computed cases."""

from __future__ import annotations

import math

import pytest

from app.eval.metrics import (
    RankedDoc,
    aggregate,
    first_ranks,
    gate,
    ndcg_at,
    recall_at,
    reciprocal_rank,
    score_run,
)


def test_first_ranks_keeps_each_documents_first_chunk_rank() -> None:
    docs = first_ranks(["a", "b", "a", "c", "b"], [0.9, 0.8, 0.7, 0.6, 0.5])
    assert [(d.key, d.rank, d.score) for d in docs] == [("a", 1, 0.9), ("b", 2, 0.8), ("c", 4, 0.6)]


DOCS = [RankedDoc("a", 1, 0.9), RankedDoc("b", 3, 0.8), RankedDoc("c", 9, 0.6)]


def test_recall_all_and_any() -> None:
    assert recall_at(DOCS, ["b", "c"], 5) == 0.5  # c is at chunk rank 9
    assert recall_at(DOCS, ["b", "c"], 10) == 1.0
    assert recall_at(DOCS, ["x", "c"], 8, match="any") == 0.0
    assert recall_at(DOCS, ["x", "c"], 9, match="any") == 1.0


def test_reciprocal_rank_uses_first_expected_chunk_rank() -> None:
    assert reciprocal_rank(DOCS, ["c", "b"]) == pytest.approx(1 / 3)
    assert reciprocal_rank(DOCS, ["x"]) == 0.0


def test_ndcg_binary_relevance() -> None:
    # Expected {b, c}: document order is a, b, c -> gains at positions 2 and 3.
    dcg = 1 / math.log2(3) + 1 / math.log2(4)
    idcg = 1 / math.log2(2) + 1 / math.log2(3)
    assert ndcg_at(DOCS, ["b", "c"]) == pytest.approx(dcg / idcg)
    # "any": one relevant document at position 2.
    assert ndcg_at(DOCS, ["b", "c"], match="any") == pytest.approx(1 / math.log2(3))
    assert ndcg_at(DOCS, ["a"]) == 1.0


def _score(**kw: object) -> object:
    base: dict[str, object] = {
        "item": "i",
        "persona": "p",
        "tags": ["t"],
        "answerable": True,
        "expected": ["a"],
        "match": "all",
        "must_not_see": [],
        "docs": DOCS,
        "readable": {"a": True, "b": True, "c": True},
        "embed_ms": 1.0,
        "search_ms": 2.0,
    }
    base.update(kw)
    return score_run(**base)  # type: ignore[arg-type]


def test_leak_by_must_not_see_and_by_oracle() -> None:
    s = _score(must_not_see=["b"])
    assert s.leaked == ["b"]  # type: ignore[attr-defined]
    s = _score(readable={"a": True, "b": True, "c": False})
    assert s.leaked == ["c"]  # type: ignore[attr-defined]
    assert _score().leaked == []  # type: ignore[attr-defined]


def test_unanswerable_scores_abstention() -> None:
    assert _score(answerable=False, expected=[]).metrics == {"abstained": 0.0}  # type: ignore[attr-defined]
    assert _score(answerable=False, expected=[], docs=[]).metrics == {"abstained": 1.0}  # type: ignore[attr-defined]


def test_aggregate_and_gate() -> None:
    good = _score()
    miss = _score(expected=["z"])
    none = _score(answerable=False, expected=[], docs=[])
    m = aggregate([good, miss, none])  # type: ignore[list-item]
    assert m["runs"] == 3 and m["answerable_runs"] == 2 and m["unanswerable_runs"] == 1
    assert m["recall@8"] == 0.5 and m["mrr"] == 0.5 and m["abstention"] == 1.0 and m["leaks"] == 0
    assert m["by_tag"]["t"]["runs"] == 2

    assert gate(m, {**m, "recall@8": 0.52}) == []  # a drop of exactly 0.02 is allowed
    reasons = gate(m, {**m, "recall@8": 0.53})
    assert len(reasons) == 1 and reasons[0].startswith("recall@8 dropped")
    leaky = aggregate([_score(must_not_see=["a"])])  # type: ignore[list-item]
    assert any(r.startswith("leakage") for r in gate(leaky, leaky))
