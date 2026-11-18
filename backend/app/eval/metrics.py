"""Evaluation metrics, at document level.

Retrieval returns chunks in rank order. A document's rank is the rank of its first chunk, so `recall@k` asks
"is the expected document in the top k chunks the chat would receive?" and `MRR` uses that same chunk rank.
`nDCG@10` uses the de-duplicated document order of the first 10 chunks (binary relevance).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

CUTOFFS = (5, 8)
NDCG_K = 10


@dataclass(frozen=True)
class RankedDoc:
    key: str
    rank: int  # 1-based rank of the document's first chunk
    score: float


def first_ranks(chunk_doc_keys: list[str], scores: list[float]) -> list[RankedDoc]:
    seen: dict[str, RankedDoc] = {}
    for i, (key, score) in enumerate(zip(chunk_doc_keys, scores, strict=True), start=1):
        if key not in seen:
            seen[key] = RankedDoc(key, i, score)
    return list(seen.values())


def recall_at(docs: list[RankedDoc], expected: list[str], k: int, match: str = "all") -> float:
    found = {d.key for d in docs if d.rank <= k} & set(expected)
    if match == "any":
        return 1.0 if found else 0.0
    return len(found) / len(expected)


def reciprocal_rank(docs: list[RankedDoc], expected: list[str]) -> float:
    ranks = [d.rank for d in docs if d.key in set(expected)]
    return 1.0 / min(ranks) if ranks else 0.0


def ndcg_at(docs: list[RankedDoc], expected: list[str], k: int = NDCG_K, match: str = "all") -> float:
    order = [d.key for d in sorted(docs, key=lambda d: d.rank) if d.rank <= k]
    relevant = set(expected)
    gains = [1.0 / math.log2(i + 2) for i, key in enumerate(order) if key in relevant]
    if match == "any":
        return gains[0] if gains else 0.0  # one relevant document is enough: ideal is rank 1
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return sum(gains) / ideal if ideal else 0.0


@dataclass
class RunScore:
    """One item asked as one persona."""

    item: str
    persona: str
    tags: list[str]
    answerable: bool
    docs: list[RankedDoc]
    leaked: list[str]
    embed_ms: float
    search_ms: float
    metrics: dict[str, float] = field(default_factory=dict)


def score_run(
    *,
    item: str,
    persona: str,
    tags: list[str],
    answerable: bool,
    expected: list[str],
    match: str,
    must_not_see: list[str],
    docs: list[RankedDoc],
    readable: dict[str, bool],
    embed_ms: float,
    search_ms: float,
) -> RunScore:
    """`readable` is the oracle's verdict for every retrieved document key."""
    retrieved = {d.key for d in docs}
    leaked = sorted((retrieved & set(must_not_see)) | {k for k in retrieved if not readable.get(k, False)})
    m: dict[str, float] = {}
    if answerable:
        for k in CUTOFFS:
            m[f"recall@{k}"] = recall_at(docs, expected, k, match)
        m["mrr"] = reciprocal_rank(docs, expected)
        m[f"ndcg@{NDCG_K}"] = ndcg_at(docs, expected, NDCG_K, match)
    else:
        m["abstained"] = 0.0 if docs else 1.0
    return RunScore(item, persona, tags, answerable, docs, leaked, embed_ms, search_ms, m)


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 4) if values else 0.0


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, math.ceil(q * len(s)) - 1)], 1)


ANSWER_METRICS = [*(f"recall@{k}" for k in CUTOFFS), "mrr", f"ndcg@{NDCG_K}"]


def aggregate(scores: list[RunScore]) -> dict[str, Any]:
    answerable = [s for s in scores if s.answerable]
    unanswerable = [s for s in scores if not s.answerable]
    out: dict[str, Any] = {
        "runs": len(scores),
        "answerable_runs": len(answerable),
        "unanswerable_runs": len(unanswerable),
        "leaks": sum(1 for s in scores if s.leaked),
        "leaked": sorted({f"{s.item}:{s.persona}:{d}" for s in scores for d in s.leaked}),
    }
    for name in ANSWER_METRICS:
        out[name] = _mean([s.metrics[name] for s in answerable])
    out["abstention"] = _mean([s.metrics["abstained"] for s in unanswerable])
    by_tag: dict[str, list[RunScore]] = defaultdict(list)
    for s in answerable:
        for t in s.tags:
            by_tag[t].append(s)
    out["by_tag"] = {
        t: {
            "runs": len(v),
            "recall@8": _mean([s.metrics["recall@8"] for s in v]),
            "mrr": _mean([s.metrics["mrr"] for s in v]),
        }
        for t, v in sorted(by_tag.items())
    }
    out["latency_ms"] = {
        "embed_p50": _pct([s.embed_ms for s in scores], 0.5),
        "embed_p95": _pct([s.embed_ms for s in scores], 0.95),
        "search_p50": _pct([s.search_ms for s in scores], 0.5),
        "search_p95": _pct([s.search_ms for s in scores], 0.95),
    }
    return out


GATE_METRICS = ("recall@8", "mrr", "abstention")


def gate(current: dict[str, Any], baseline: dict[str, Any], max_drop: float = 0.02) -> list[str]:
    """Reasons the gate fails (empty list = pass). Leakage always fails, whatever the baseline says."""
    reasons = []
    if current["leaks"]:
        reasons.append(f"leakage: {current['leaks']} run(s) retrieved documents the persona may not read")
    for name in GATE_METRICS:
        drop = baseline[name] - current[name]
        if drop > max_drop + 1e-9:
            reasons.append(f"{name} dropped {drop:.4f} (baseline {baseline[name]:.4f}, now {current[name]:.4f})")
    return reasons
