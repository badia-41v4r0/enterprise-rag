"""The evaluation end to end on the real retrieval path, with the recorded real embeddings (no Ollama needed),
and the guard that keeps the evaluation corpus out of the application database."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.eval.metrics import gate
from app.eval.prepare import EvalDatabaseError, build_embedder, load_corpus, switch_database
from app.eval.runner import run_set
from app.eval.schema import EvalSet, load_set
from app.models import (
    Action,
    Container,
    EvaluationResult,
    EvaluationRun,
    Grant,
    ResourceType,
    Role,
)


@pytest.mark.parametrize("kind", ["eval", "bench"])
def test_refuses_to_use_the_application_database(kind: str) -> None:
    var = f"ERAG_{kind.upper()}_DATABASE_URL"
    before = os.environ.get(var)
    app_url = os.environ["ERAG_DATABASE_URL"]
    os.environ[var] = app_url.replace("localhost", "127.0.0.1") if "localhost" not in app_url else app_url
    get_settings.cache_clear()
    try:
        with pytest.raises(EvalDatabaseError, match="application database"):
            switch_database(kind)  # type: ignore[arg-type]
        assert os.environ["ERAG_DATABASE_URL"] == app_url
    finally:
        if before is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = before
        get_settings.cache_clear()


def _subset(full: EvalSet, ids: set[str]) -> EvalSet:
    return EvalSet(name="acme-mini", items=[i for i in full.items if i.id in ids], digest="mini")


MINI = {"hw-001", "hw-020", "hw-040", "hw-044", "hw-052", "gen-ct-002", "gen-err-001", "gen-ax-001", "hw-080"}


@pytest.mark.db
async def test_mini_set_end_to_end_and_a_planted_leak_fails_the_gate(db: AsyncSession) -> None:
    embedder = build_embedder("strict")
    stats = await load_corpus(embedder)
    assert stats["versions"] == stats["indexed"] > 250 and stats["chunks"] > 600

    mini = _subset(load_set("acme"), MINI)
    report = await run_set(db, mini, embedder, label="test")
    m = report["metrics"]
    assert m["leaks"] == 0
    assert m["answerable_runs"] > 0 and m["recall@8"] > 0.5
    run = await db.get(EvaluationRun, __import__("uuid").UUID(report["run_id"]))
    assert run is not None and run.status == "done" and run.config["set_digest"] == "mini"
    rows = (await db.scalars(select(EvaluationResult).where(EvaluationResult.run_id == run.id))).all()
    assert len(rows) == m["runs"]
    # Results hold document keys and scores, never text.
    assert all(set(d) == {"key", "rank", "score"} for r in rows for d in r.retrieved["documents"])

    # Plant a leak: Employee may now read Sales Contracts. The oracle (declared structure) disagrees.
    employee = await db.scalar(select(Role).where(Role.name == "Employee"))
    sales = await db.scalar(select(Container).where(Container.slug == "sales-contracts"))
    assert employee is not None and sales is not None
    db.add(Grant(role_id=employee.id, resource_type=ResourceType.container, resource_id=sales.id, action=Action.read))
    await db.commit()
    leaky = await run_set(db, mini, embedder, label="leak")
    assert leaky["metrics"]["leaks"] > 0
    assert any("sales-contracts/" in leak for leak in leaky["metrics"]["leaked"])
    assert any(r.startswith("leakage") for r in gate(leaky["metrics"], report["metrics"]))
