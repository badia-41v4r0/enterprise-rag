"""Runtime retrieval settings (Admin > Retrieval), stored in `app_settings` under `retrieval` (R14).

They change ranking only. The authorization filter, the ACL recheck and classification routing do not depend
on them.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.ml.models import MODELS
from app.services.settings_store import get_setting, put_setting

KEY = "retrieval"
Mode = Literal["vector", "hybrid"]


@dataclass(frozen=True)
class RetrievalSettings:
    mode: Mode = "hybrid"
    vector_k: int = 40  # candidates from each leg before fusion
    text_k: int = 40
    rrf_k: int = 60
    vector_weight: float = 1.0
    text_weight: float = 1.0
    # Empty string: no reranker. Otherwise a model name from app.ml.models (kind "reranker").
    reranker: str = "mmarco-minilm-l12"
    rerank_candidates: int = 30
    rerank_min_score: float = 0.02  # calibrated on the acme set (R14, docs/benchmarks/r14-retrieval.md)
    # Without a reranker (none configured, or the fallback): cosine floor for vector hits; text-only hits need a
    # normalized ts_rank_cd of at least `text_min_rank`.
    vector_min_score: float = 0.45
    text_min_rank: float = 0.05
    final_k: int = 8

    def validate(self) -> RetrievalSettings:
        problems = []
        if self.mode not in ("vector", "hybrid"):
            problems.append("mode must be vector or hybrid")
        for name in ("vector_k", "text_k", "rerank_candidates", "final_k"):
            v = getattr(self, name)
            if not 1 <= v <= 200:
                problems.append(f"{name} must be between 1 and 200")
        if not 1 <= self.rrf_k <= 1000:
            problems.append("rrf_k must be between 1 and 1000")
        for name in ("vector_weight", "text_weight"):
            if not 0 <= getattr(self, name) <= 10:
                problems.append(f"{name} must be between 0 and 10")
        for name in ("rerank_min_score", "vector_min_score", "text_min_rank"):
            if not 0 <= getattr(self, name) <= 1:
                problems.append(f"{name} must be between 0 and 1")
        if self.reranker and (self.reranker not in MODELS or MODELS[self.reranker].kind != "reranker"):
            problems.append(f"unknown reranker {self.reranker}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def merged(self, values: dict[str, Any]) -> RetrievalSettings:
        fields = {f.name: f.type for f in dataclasses.fields(self)}
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key not in fields:
                raise ValueError(f"unknown retrieval setting {key}")
            current = getattr(self, key)
            if isinstance(current, bool) or not isinstance(current, int | float):
                clean[key] = str(value) if not isinstance(value, str) else value
            elif isinstance(current, int):
                clean[key] = int(value)
            else:
                clean[key] = float(value)
        return dataclasses.replace(self, **clean).validate()

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def defaults() -> RetrievalSettings:
    s = get_settings()
    return RetrievalSettings(vector_min_score=s.retrieval_min_score, final_k=s.retrieval_top_k)


async def load(db: AsyncSession) -> RetrievalSettings:
    stored = await get_setting(db, KEY)
    try:
        return defaults().merged(stored)
    except ValueError:
        return defaults()  # a stored value that no longer validates never breaks retrieval


async def save(db: AsyncSession, values: dict[str, Any], user_id: Any) -> RetrievalSettings:
    new = defaults().merged(values)
    await put_setting(db, KEY, {k: v for k, v in new.as_dict().items() if v != getattr(defaults(), k)}, user_id)
    return new
