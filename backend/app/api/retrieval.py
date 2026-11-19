"""Admin > Retrieval: runtime retrieval settings and the state of the local models (R14, ADR 0046/0047).

The settings change ranking only; the authorization filter, the ACL recheck and classification routing do not
depend on them, so changing them needs an administrator but not step-up. Every change is audited with the old
and new values. Models are installed from the command line (`erag models install`), never from the browser.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, require_admin
from app.core.db import get_db
from app.ml.models import MODELS, installed
from app.rag import reranker
from app.rag import settings as retrieval_settings
from app.services.audit import audit

router = APIRouter(prefix="/api/admin/retrieval", tags=["retrieval"])


def _models() -> list[dict[str, Any]]:
    return [
        {
            "name": m.name,
            "kind": m.kind,
            "size_mb": m.size_mb,
            "license": m.license,
            "description": m.description,
            "installed": installed(m.name),
            **({"status": reranker.status(m.name)} if m.kind == "reranker" else {}),
        }
        for m in MODELS.values()
    ]


@router.get("")
async def get_retrieval(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    current = await retrieval_settings.load(db)
    return {
        "settings": current.as_dict(),
        "defaults": retrieval_settings.defaults().as_dict(),
        "models": _models(),
        "warnings": (
            [f"The configured reranker {current.reranker} is not installed: answers use the fused order."]
            if current.reranker and not installed(current.reranker)
            else []
        ),
    }


@router.put("")
async def put_retrieval(
    body: dict[str, Any], auth: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    old = await retrieval_settings.load(db)
    try:
        new = await retrieval_settings.save(db, body, auth.user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    changed = {k: [v, new.as_dict()[k]] for k, v in old.as_dict().items() if new.as_dict()[k] != v}
    audit(
        db, "admin.retrieval.update", user_id=auth.user.id, resource="settings:retrieval", details={"changed": changed}
    )
    await db.commit()
    return await get_retrieval(auth, db)
