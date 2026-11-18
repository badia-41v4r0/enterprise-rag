"""Run an evaluation set against the retrieval path the chat uses, as each persona, and record the results."""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, effective_permissions
from app.core.config import REPO_ROOT, get_settings
from app.eval.corpus import Oracle
from app.eval.embed_cache import CachedEmbedder
from app.eval.metrics import NDCG_K, RunScore, aggregate, first_ranks, score_run
from app.eval.schema import EvalSet
from app.ingestion.embedder import Embedder
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
from app.rag.vector_store import HNSW_EF_SEARCH

REPORT_VERSION = 1


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


def config_snapshot(eval_set: EvalSet, embedder: Embedder) -> dict[str, Any]:
    s = get_settings()
    snap: dict[str, Any] = {
        "retrieval": "vector",
        "embed_model": s.embed_model,
        "embed_dim": s.embed_dim,
        "retrieval_top_k": s.retrieval_top_k,
        "retrieval_min_score": s.retrieval_min_score,
        "eval_k": max(NDCG_K, s.retrieval_top_k),
        "hnsw_ef_search": HNSW_EF_SEARCH,
        "set": eval_set.name,
        "set_digest": eval_set.digest,
    }
    if isinstance(embedder, CachedEmbedder):
        snap["embeddings"] = f"recorded ({embedder.mode})"
    return snap


async def _register_set(db: AsyncSession, eval_set: EvalSet) -> EvaluationSet:
    row = await db.scalar(select(EvaluationSet).where(EvaluationSet.name == eval_set.name))
    if row is None:
        row = EvaluationSet(name=eval_set.name, source="repo")
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


async def run_set(db: AsyncSession, eval_set: EvalSet, embedder: Embedder, *, label: str = "") -> dict[str, Any]:
    """Ask every item as every listed persona; store the run and return the report."""
    settings = get_settings()
    k = max(NDCG_K, settings.retrieval_top_k)
    oracle = Oracle()
    set_row = await _register_set(db, eval_set)
    config = config_snapshot(eval_set, embedder)
    run = EvaluationRun(set_id=set_row.id, label=label, config=config, commit=git_commit())
    db.add(run)
    await db.flush()

    keys = await _doc_keys(db)
    perms: dict[str, EffectivePermissions] = {}
    retriever = Retriever(db, embedder)
    scores: list[RunScore] = []
    for item, persona, exp in eval_set.runs():
        if persona not in perms:
            user = await db.scalar(select(User).where(User.email == persona))
            if user is None:
                raise RuntimeError(f"persona {persona} is not in the evaluation database; run `erag eval prepare`")
            perms[persona] = await effective_permissions(db, user)
        t0 = time.perf_counter()
        vector = (await embedder.embed([item.question]))[0]
        t1 = time.perf_counter()
        result = await retriever.retrieve_by_vector(perms[persona], vector, k, settings.retrieval_min_score)
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
        scores.append(score)
        db.add(
            EvaluationResult(
                run_id=run.id,
                item_key=item.id,
                persona=persona,
                retrieved={"documents": [asdict(d) for d in docs]},
                metrics=score.metrics,
                leaked=bool(score.leaked),
                embed_ms=score.embed_ms,
                search_ms=score.search_ms,
            )
        )

    metrics = aggregate(scores)
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
