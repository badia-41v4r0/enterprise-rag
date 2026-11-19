"""Admin > Evaluation (R15, ADR 0048): evaluation sets, runs against the live installation, the review queue of
user feedback, and applying a better retrieval configuration.

Who sees what:
- An administrator is not given data access by the role (ADR 0009). A feedback entry shows its question, answer
  and sources only to an administrator whose own permissions cover every cited document; others see that it
  exists and cannot review it.
- Run results name documents only when the viewing administrator can read them; other keys are hidden.
- Company sets are classified `confidential` by default and never leave the installation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth, require_admin
from app.authz.policy import EffectivePermissions, can_read_document, effective_permissions
from app.core.db import get_db, get_sessionmaker
from app.eval.live import document_map, live_oracle, load_db_set, store_items, validate_live
from app.eval.runner import compare, run_set
from app.eval.schema import EvalItem, EvalSetError, load_set
from app.models import (
    AnswerFeedback,
    Conversation,
    EvaluationItem,
    EvaluationResult,
    EvaluationRun,
    EvaluationSet,
    Message,
    User,
)
from app.rag import settings as retrieval_settings
from app.services.audit import audit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/eval", tags=["evaluation"])
COMPANY_SET = "company"
_tasks: set[asyncio.Task[None]] = set()


# --- helpers -----------------------------------------------------------------------------------------------


class _Visibility:
    """What the viewing administrator may see of document keys."""

    def __init__(self, perms: EffectivePermissions, docs: dict[str, tuple[uuid.UUID, uuid.UUID, bool]]) -> None:
        self.perms, self.docs = perms, docs

    def can(self, key: str) -> bool:
        d = self.docs.get(key)
        return d is not None and can_read_document(self.perms, container_id=d[1], document_id=d[0], restricted=d[2])

    def show(self, key: str) -> str:
        return key if self.can(key) else "(hidden)"


async def _visibility(db: AsyncSession, auth: AuthContext) -> _Visibility:
    return _Visibility(await effective_permissions(db, auth.user), await document_map(db))


async def _company_set(db: AsyncSession) -> EvaluationSet:
    row = await db.scalar(select(EvaluationSet).where(EvaluationSet.name == COMPANY_SET))
    if row is None:
        row = EvaluationSet(name=COMPANY_SET, source="db")
        db.add(row)
        await db.flush()
    return row


# --- sets ---------------------------------------------------------------------------------------------------


@router.get("/sets")
async def list_sets(
    _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = (await db.scalars(select(EvaluationSet).order_by(EvaluationSet.name))).all()
    counts = dict((await db.execute(select(EvaluationItem.set_id, func.count()).group_by(EvaluationItem.set_id))).all())
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "source": r.source,
            "classification": r.classification.value,
            "items": counts.get(r.id, 0),
        }
        for r in rows
    ]


class SetUpload(BaseModel):
    yaml_text: str = Field(max_length=2_000_000)


@router.post("/sets/{name}/items")
async def upload_items(
    name: str, body: SetUpload, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Add or replace items of a company set from YAML (same format as eval/sets/*.yaml, without `include`)."""
    if not name.replace("-", "").replace("_", "").isalnum() or len(name) > 60:
        raise HTTPException(422, "set names use letters, digits, - and _")
    try:
        data = yaml.safe_load(body.yaml_text) or {}
        items = [EvalItem.model_validate(i) for i in (data.get("items") or [])]
        if not items:
            raise EvalSetError("no items")
        await validate_live(db, items)
    except (yaml.YAMLError, ValueError, EvalSetError) as exc:
        raise HTTPException(422, str(exc)[:4000]) from exc
    row = await db.scalar(select(EvaluationSet).where(EvaluationSet.name == name))
    if row is None:
        row = EvaluationSet(name=name, source="db")
        db.add(row)
        await db.flush()
    elif row.source != "db":
        raise HTTPException(409, "repository sets are edited in the repository, not here")
    n = await store_items(db, row, items)
    audit(db, "admin.eval.items_uploaded", user_id=auth.user.id, resource=f"eval_set:{row.id}", details={"items": n})
    await db.commit()
    return {"set": name, "items": n}


# --- runs ---------------------------------------------------------------------------------------------------


class RunIn(BaseModel):
    set: str
    label: str = Field(default="", max_length=100)
    overrides: dict[str, Any] = Field(default_factory=dict)


async def _execute(set_name: str, label: str, overrides: dict[str, Any], user_id: uuid.UUID) -> None:
    from app.ingestion.embedder import get_embedder

    async with get_sessionmaker()() as db:
        try:
            row = await db.scalar(select(EvaluationSet).where(EvaluationSet.name == set_name))
            if row is None:
                raise EvalSetError(f"no set {set_name}")
            eval_set = await load_db_set(db, row) if row.source == "db" else load_set(set_name, validate=False)
            oracle = await live_oracle(db, {u for i in eval_set.items for u in i.as_users})
            cfg = (await retrieval_settings.load(db)).merged(overrides) if overrides else None
            await run_set(db, eval_set, get_embedder(), label=label, retrieval=cfg, oracle=oracle, source=row.source)
        except Exception:
            log.exception("evaluation run failed")
            await db.rollback()
            audit(db, "admin.eval.run_failed", user_id=user_id, decision="error", details={"set": set_name})
            await db.commit()


@router.post("/runs", status_code=202)
async def start_run(
    body: RunIn, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Run a set as its personas against this installation (live embeddings, current or candidate settings)."""
    if any(not t.done() for t in _tasks):
        raise HTTPException(409, "an evaluation run is already in progress")
    row = await db.scalar(select(EvaluationSet).where(EvaluationSet.name == body.set))
    if row is None:
        if body.set != "acme":
            raise HTTPException(404, "no such set")
        raise HTTPException(409, "the acme set runs against the evaluation database: use `erag eval run`")
    try:
        if body.overrides:
            (await retrieval_settings.load(db)).merged(body.overrides)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        "admin.eval.run_started",
        user_id=auth.user.id,
        resource=f"eval_set:{row.id}",
        details={"label": body.label, "overrides": body.overrides},
    )
    await db.commit()
    task = asyncio.create_task(_execute(body.set, body.label, body.overrides, auth.user.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"status": "started"}


@router.get("/runs")
async def list_runs(
    _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(EvaluationRun, EvaluationSet.name)
            .join(EvaluationSet, EvaluationSet.id == EvaluationRun.set_id)
            .order_by(EvaluationRun.started_at.desc())
            .limit(50)
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "set": name,
            "label": r.label,
            "status": r.status,
            "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "metrics": {k: v for k, v in r.metrics.items() if k not in ("leaked", "by_tag")},
            "retrieval": r.config.get("retrieval", {}),
            "running": any(not t.done() for t in _tasks) and r.status == "running",
        }
        for r, name in rows
    ]


async def _report(db: AsyncSession, run_id: uuid.UUID, vis: _Visibility) -> dict[str, Any]:
    run = await db.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    items = {
        i.key: i for i in (await db.scalars(select(EvaluationItem).where(EvaluationItem.set_id == run.set_id))).all()
    }
    results = (await db.scalars(select(EvaluationResult).where(EvaluationResult.run_id == run.id))).all()
    out = []
    for r in results:
        payload = items[r.item_key].payload if r.item_key in items else {}
        answerable = bool(payload.get("expect", {}).get(r.persona, {}).get("answerable", "abstained" not in r.metrics))
        out.append(
            {
                "item": r.item_key,
                "persona": r.persona,
                "tags": payload.get("tags", []),
                "answerable": answerable,
                "metrics": r.metrics,
                "leaked": [vis.show(k) for k in r.retrieved.get("leaked", [])] if r.leaked else [],
                "documents": [[vis.show(d["key"]), d["rank"], d["score"]] for d in r.retrieved.get("documents", [])],
            }
        )
    metrics = dict(run.metrics)
    metrics["leaked"] = [m.rsplit(":", 1)[0] + ":" + vis.show(m.rsplit(":", 1)[1]) for m in metrics.get("leaked", [])]
    return {"run_id": str(run.id), "label": run.label, "config": run.config, "metrics": metrics, "results": out}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return await _report(db, run_id, await _visibility(db, auth))


@router.get("/compare")
async def compare_runs(
    a: uuid.UUID, b: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    vis = await _visibility(db, auth)
    diff = compare(await _report(db, a, vis), await _report(db, b, vis))
    return {
        "metrics": {k: {"a": va, "b": vb, "delta": d} for k, (va, vb, d) in diff["metrics"].items()},
        "by_tag": {k: {"a": va, "b": vb} for k, (va, vb) in diff["by_tag"].items()},
        "better": [dict(zip(("item", "persona", "metric", "a", "b"), r, strict=True)) for r in diff["better"]],
        "worse": [dict(zip(("item", "persona", "metric", "a", "b"), r, strict=True)) for r in diff["worse"]],
    }


@router.post("/runs/{run_id}/apply")
async def apply_run_settings(
    run_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Make the retrieval settings a run was measured with the installation's settings."""
    run = await db.get(EvaluationRun, run_id)
    if run is None or run.status != "done":
        raise HTTPException(404, "no finished run")
    if run.metrics.get("leaks"):
        raise HTTPException(409, "a run with leaks cannot be applied")
    values = dict(run.config.get("retrieval", {}))
    values["final_k"] = (await retrieval_settings.load(db)).final_k  # runs measure a deeper k than the chat uses
    try:
        new = await retrieval_settings.save(db, values, auth.user.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        "admin.retrieval.update",
        user_id=auth.user.id,
        resource="settings:retrieval",
        details={"from_run": str(run.id)},
    )
    await db.commit()
    return {"settings": new.as_dict()}


# --- feedback review queue --------------------------------------------------------------------------------


async def _feedback_view(db: AsyncSession, fb: AnswerFeedback, vis: _Visibility) -> dict[str, Any]:
    answer = await db.get(Message, fb.message_id)
    asker = await db.get(User, fb.user_id)
    cited = (answer.citations or {}).get("items", []) if answer else []
    doc_ids = {c.get("document_id") for c in cited}
    keys = [k for k, (doc_id, _c, _r) in vis.docs.items() if str(doc_id) in doc_ids]
    reviewable = answer is not None and all(vis.can(k) for k in keys)
    view: dict[str, Any] = {
        "id": str(fb.id),
        "rating": fb.rating,
        "state": fb.review_state,
        "created_at": fb.created_at.isoformat(),
        "asker": asker.email if asker else None,
        "reviewable": reviewable,
    }
    if reviewable and answer is not None:
        question = await db.scalar(
            select(Message.content)
            .where(
                Message.conversation_id == answer.conversation_id,
                Message.role == "user",
                Message.created_at <= answer.created_at,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        view.update(question=question, answer=answer.content, comment=fb.comment, cited_documents=sorted(keys))
    return view


@router.get("/feedback")
async def feedback_queue(
    state: Literal["proposed", "accepted", "rejected"] = "proposed",
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    vis = await _visibility(db, auth)
    rows = (
        await db.scalars(
            select(AnswerFeedback)
            .where(AnswerFeedback.review_state == state)
            .order_by(AnswerFeedback.created_at.desc())
            .limit(200)
        )
    ).all()
    return [await _feedback_view(db, fb, vis) for fb in rows]


class AcceptIn(BaseModel):
    documents: list[str] = Field(default_factory=list, max_length=20)
    answerable: bool = True
    question: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=lambda: ["feedback"], max_length=10)


@router.post("/feedback/{feedback_id}/accept")
async def accept_feedback(
    feedback_id: uuid.UUID,
    body: AcceptIn,
    auth: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    fb = await db.get(AnswerFeedback, feedback_id)
    if fb is None or fb.review_state != "proposed":
        raise HTTPException(404, "no proposed feedback")
    vis = await _visibility(db, auth)
    view = await _feedback_view(db, fb, vis)
    if not view["reviewable"]:
        raise HTTPException(403, "you cannot read every source of this answer")
    if any(not vis.can(d) for d in body.documents):
        raise HTTPException(403, "you can only name documents you can read")
    asker = view["asker"]
    item = EvalItem.model_validate(
        {
            "id": f"fb-{str(fb.id)[:8]}",
            "question": body.question or view["question"],
            "as_users": [asker],
            "expect": {asker: {"documents": body.documents, "answerable": body.answerable}},
            "tags": body.tags,
        }
    )
    try:
        await validate_live(db, [item])
    except EvalSetError as exc:
        raise HTTPException(422, str(exc)) from exc
    set_row = await _company_set(db)
    await store_items(db, set_row, [item])
    stored = await db.scalar(
        select(EvaluationItem).where(EvaluationItem.set_id == set_row.id, EvaluationItem.key == item.id)
    )
    fb.review_state, fb.reviewed_by, fb.reviewed_at = "accepted", auth.user.id, datetime.now(UTC)
    fb.eval_item_id = stored.id if stored else None
    audit(
        db,
        "admin.eval.feedback_accepted",
        user_id=auth.user.id,
        resource=f"feedback:{fb.id}",
        details={"item": item.id},
    )
    await db.commit()
    return {"item": item.id, "set": COMPANY_SET}


@router.post("/feedback/{feedback_id}/reject")
async def reject_feedback(
    feedback_id: uuid.UUID, auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    fb = await db.get(AnswerFeedback, feedback_id)
    if fb is None or fb.review_state != "proposed":
        raise HTTPException(404, "no proposed feedback")
    fb.review_state, fb.reviewed_by, fb.reviewed_at = "rejected", auth.user.id, datetime.now(UTC)
    audit(db, "admin.eval.feedback_rejected", user_id=auth.user.id, resource=f"feedback:{fb.id}")
    await db.commit()
    return {"state": "rejected"}


# --- the chat side: users rate their own answers -------------------------------------------------------------

chat_router = APIRouter(prefix="/api/chat", tags=["chat"])


class FeedbackIn(BaseModel):
    message_id: uuid.UUID
    rating: Literal[-1, 1]
    comment: str = Field(default="", max_length=2000)
    propose: bool = False


@chat_router.post("/feedback")
async def give_feedback(
    body: FeedbackIn, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    msg = await db.get(Message, body.message_id)
    conv = await db.get(Conversation, msg.conversation_id) if msg else None
    if msg is None or conv is None or conv.user_id != auth.user.id or msg.role != "assistant":
        raise HTTPException(404, "no such answer")
    fb = await db.scalar(
        select(AnswerFeedback).where(AnswerFeedback.message_id == msg.id, AnswerFeedback.user_id == auth.user.id)
    )
    if fb is None:
        fb = AnswerFeedback(message_id=msg.id, user_id=auth.user.id, rating=body.rating)
        db.add(fb)
    fb.rating, fb.comment = body.rating, body.comment
    if body.propose and body.rating < 0 and fb.review_state is None:
        fb.review_state = "proposed"
    audit(
        db,
        "chat.feedback",
        user_id=auth.user.id,
        resource=f"message:{msg.id}",
        details={"rating": body.rating, "proposed": fb.review_state == "proposed"},  # never the comment text
    )
    await db.commit()
    return {"rating": fb.rating, "state": fb.review_state}
