"""Run an evaluation set against the retrieval path the chat uses, as each persona, and record the results."""

from __future__ import annotations

import dataclasses
import subprocess
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, effective_permissions
from app.core.config import REPO_ROOT, get_settings
from app.eval.answers import answer_metrics
from app.eval.corpus import Oracle
from app.eval.embed_cache import CachedEmbedder
from app.eval.metrics import NDCG_K, RunScore, aggregate, first_ranks, score_run
from app.eval.schema import EvalSet
from app.ingestion.embedder import Embedder
from app.llm.providers.base import LLMProvider
from app.models import (
    Container,
    Document,
    EvaluationItem,
    EvaluationResult,
    EvaluationRun,
    EvaluationSet,
    User,
)
from app.rag.retrieval import Retriever
from app.rag.settings import RetrievalSettings
from app.rag.settings import load as load_retrieval_settings
from app.rag.vector_store import HNSW_EF_SEARCH

REPORT_VERSION = 1


class ReadOracle(Protocol):
    def can_read(self, email: str, doc_key: str) -> bool: ...


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed command, optional metadata
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def config_snapshot(eval_set: EvalSet, embedder: Embedder, retrieval: RetrievalSettings) -> dict[str, Any]:
    s = get_settings()
    snap: dict[str, Any] = {
        "retrieval": retrieval.as_dict(),
        "embed_model": s.embed_model,
        "embed_dim": s.embed_dim,
        "eval_k": max(NDCG_K, retrieval.final_k),
        "hnsw_ef_search": HNSW_EF_SEARCH,
        "set": eval_set.name,
        "set_digest": eval_set.digest,
    }
    if isinstance(embedder, CachedEmbedder):
        snap["embeddings"] = f"recorded ({embedder.mode})"
    return snap


async def _register_set(db: AsyncSession, eval_set: EvalSet, source: str = "repo") -> EvaluationSet:
    row = await db.scalar(select(EvaluationSet).where(EvaluationSet.name == eval_set.name))
    if row is not None and source == "db":
        return row  # a company set is edited through its items, never replaced from a file
    if row is None:
        row = EvaluationSet(name=eval_set.name, source=source)
        db.add(row)
        await db.flush()
    if row.digest != eval_set.digest:
        row.digest = eval_set.digest
        existing = {i.key: i for i in (await db.scalars(select(EvaluationItem).where(EvaluationItem.set_id == row.id)))}
        for item in eval_set.items:
            payload = item.model_dump(include={"as_users", "expect", "tags"})
            rec = existing.pop(item.id, None)
            if rec is None:
                db.add(EvaluationItem(set_id=row.id, key=item.id, question=item.question, payload=payload))
            else:
                rec.question, rec.payload = item.question, payload
        for stale in existing.values():
            await db.delete(stale)
        await db.flush()
    return row


async def _doc_keys(db: AsyncSession) -> dict[uuid.UUID, str]:
    rows = (await db.execute(select(Document.id, Container.slug, Document.filename).join(Container))).all()
    return {doc_id: f"{slug}/{filename}" for doc_id, slug, filename in rows}


async def run_set(
    db: AsyncSession,
    eval_set: EvalSet,
    embedder: Embedder,
    *,
    label: str = "",
    retrieval: RetrievalSettings | None = None,
    oracle: ReadOracle | None = None,
    source: str = "repo",
    answers: LLMProvider | None = None,
) -> dict[str, Any]:
    """Ask every item as every listed persona; store the run and return the report.

    `retrieval` defaults to the evaluation database's stored settings (Admin > Retrieval there). The report
    measures the top `max(10, final_k)` so nDCG@10 is defined; the chat uses `final_k`."""
    cfg = retrieval or await load_retrieval_settings(db)
    k = max(NDCG_K, cfg.final_k)
    run_cfg = dataclasses.replace(cfg, final_k=k)
    oracle = oracle or Oracle()
    set_row = await _register_set(db, eval_set, source)
    config = config_snapshot(eval_set, embedder, cfg)
    run = EvaluationRun(set_id=set_row.id, label=label, config=config, commit=git_commit())
    db.add(run)
    await db.flush()

    keys = await _doc_keys(db)
    perms: dict[str, EffectivePermissions] = {}
    retriever = Retriever(db, embedder)
    scores: list[RunScore] = []
    fallbacks = 0
    for item, persona, exp in eval_set.runs():
        if persona not in perms:
            user = await db.scalar(select(User).where(User.email == persona))
            if user is None:
                raise RuntimeError(f"persona {persona} is not in the evaluation database; run `erag eval prepare`")
            perms[persona] = await effective_permissions(db, user)
        t0 = time.perf_counter()
        vector = (await embedder.embed([item.question]))[0]
        t1 = time.perf_counter()
        result = await retriever.retrieve(perms[persona], item.question, settings=run_cfg, vector=vector)
        fallbacks += result.reranker_fallback is not None
        t2 = time.perf_counter()
        doc_keys = [keys[h.document_id] for h in result.hits]
        docs = first_ranks(doc_keys, [h.score for h in result.hits])
        score = score_run(
            item=item.id,
            persona=persona,
            tags=item.tags,
            answerable=bool(exp.answerable),
            expected=exp.documents,
            match=exp.match,
            must_not_see=exp.must_not_see,
            docs=docs,
            readable={d.key: oracle.can_read(persona, d.key) for d in docs},
            embed_ms=(t1 - t0) * 1000,
            search_ms=(t2 - t1) * 1000,
        )
        if answers is not None and score.answerable:
            score.metrics.update(await answer_metrics(answers, item.question, result.hits, keys, exp.documents))
        scores.append(score)
        db.add(
            EvaluationResult(
                run_id=run.id,
                item_key=item.id,
                persona=persona,
                retrieved={"documents": [asdict(d) for d in docs], "leaked": score.leaked},
                metrics=score.metrics,
                leaked=bool(score.leaked),
                embed_ms=score.embed_ms,
                search_ms=score.search_ms,
            )
        )

    metrics = aggregate(scores)
    metrics["reranker_fallbacks"] = fallbacks
    if answers is not None:
        judged = [s.metrics for s in scores if "faithful" in s.metrics]
        metrics["answers"] = {
            "model": answers.model_id,
            "judged": len(judged),
            "citation_precision": round(sum(m["citation_precision"] for m in judged) / max(1, len(judged)), 4),
            "faithfulness": round(sum(m["faithful"] for m in judged) / max(1, len(judged)), 4),
        }
    run.metrics = metrics
    run.status = "done"
    run.finished_at = datetime.now(UTC)
    await db.commit()
    return {
        "version": REPORT_VERSION,
        "run_id": str(run.id),
        "label": label,
        "commit": run.commit,
        "finished_at": run.finished_at.isoformat(),
        "config": config,
        "metrics": metrics,
        "results": [
            {
                "item": s.item,
                "persona": s.persona,
                "tags": s.tags,
                "answerable": s.answerable,
                "metrics": s.metrics,
                "leaked": s.leaked,
                "documents": [[d.key, d.rank, round(d.score, 4)] for d in s.docs],
            }
            for s in scores
        ],
    }


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """What changed from report `a` to report `b`: metric deltas, per-tag deltas and per-run changes."""
    ma, mb = a["metrics"], b["metrics"]
    names = ["recall@5", "recall@8", "mrr", f"ndcg@{NDCG_K}", "abstention", "leaks"]
    out: dict[str, Any] = {
        "metrics": {n: (ma.get(n, 0), mb.get(n, 0), round(mb.get(n, 0) - ma.get(n, 0), 4)) for n in names}
    }
    tags = sorted(set(ma.get("by_tag", {})) | set(mb.get("by_tag", {})))
    out["by_tag"] = {
        t: (
            ma.get("by_tag", {}).get(t, {}).get("recall@8", 0),
            mb.get("by_tag", {}).get(t, {}).get("recall@8", 0),
        )
        for t in tags
    }
    ra = {(r["item"], r["persona"]): r for r in a["results"]}
    better, worse = [], []
    for r in b["results"]:
        prev = ra.get((r["item"], r["persona"]))
        if prev is None:
            continue
        key = "mrr" if r["answerable"] else "abstained"
        before, after = prev["metrics"].get(key, 0), r["metrics"].get(key, 0)
        if after > before:
            better.append((r["item"], r["persona"], key, before, after))
        elif after < before:
            worse.append((r["item"], r["persona"], key, before, after))
    out["better"], out["worse"] = better, worse
    return out
