"""Measure filtered vector search on the synthetic benchmark corpus (load it with scripts/synth_corpus.py).

    uv run python scripts/bench_search.py --queries 50 --out docs/benchmarks/r13-baseline.md

For users who can read 90, 10 and 1 percent of the containers, and for k = 8 and 50, it runs the production
search (`PgVectorStore.search`, HNSW with iterative scan and the authorization filter) and reports p50/p95
latency, how often fewer than k rows came back, and recall against an exact scan with the same filter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.eval.prepare import EvalDatabaseError, switch_database
from app.rag.vector_store import HNSW_EF_SEARCH, AccessFilter, PgVectorStore

PATTERNS = {"90%": 450, "10%": 50, "1%": 5}  # number of the 500 containers the user can read


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(q * len(s)) - 1))]


def hardware() -> str:
    cpu = platform.processor() or platform.machine()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
        mem_kb = next(
            int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemTotal")
        )
        mem = f"{mem_kb / 1024 / 1024:.0f} GB RAM"
    except (OSError, StopIteration, ValueError):
        mem = "unknown RAM"
    return f"{cpu}, {os.cpu_count()} threads, {mem}"


async def run(queries: int, exact_queries: int, seed: int) -> tuple[str, list[str]]:
    dim = get_settings().embed_dim
    rng = np.random.default_rng(seed)
    sm = get_sessionmaker()
    async with sm() as db:
        containers = [r[0] for r in (await db.execute(text("SELECT id FROM containers ORDER BY slug"))).all()]
        n_chunks = await db.scalar(text("SELECT count(*) FROM chunks"))
        info = (
            await db.execute(text("SELECT version(), (SELECT extversion FROM pg_extension WHERE extname = 'vector')"))
        ).one()
        # Real-looking queries: points near stored vectors, not uniform noise.
        sample = (
            await db.execute(text("SELECT embedding FROM chunks TABLESAMPLE SYSTEM (1) LIMIT :n"), {"n": queries})
        ).all()
    # A raw text() query returns pgvector values in their text form ('[0.1,...]').
    base = [np.array(json.loads(r[0]) if isinstance(r[0], str) else r[0], dtype=np.float32) for r in sample]
    qvecs = []
    for v in base:
        q = v + 0.3 * rng.standard_normal(dim).astype(np.float32)
        qvecs.append((q / np.linalg.norm(q)).tolist())

    rows = []
    for name, n in PATTERNS.items():
        access = AccessFilter(frozenset(containers[:n]), frozenset())
        for k in (8, 50):
            lat, short, recalls = [], 0, []
            for i, q in enumerate(qvecs):
                async with sm() as db:
                    t0 = time.perf_counter()
                    hits = await PgVectorStore(db).search(q, access, k)
                    lat.append((time.perf_counter() - t0) * 1000)
                    await db.rollback()
                short += len(hits) < k
                if i < exact_queries:
                    async with sm() as db:
                        await db.execute(text("SET LOCAL enable_indexscan = off"))
                        exact = await PgVectorStore(db).search(q, access, k)
                        await db.rollback()
                    want = {h.chunk_id for h in exact}
                    if want:
                        recalls.append(len(want & {h.chunk_id for h in hits}) / len(want))
            rows.append(
                f"| {name} | {k} | {statistics.median(lat):.1f} | {pct(lat, 0.95):.1f} | {short}/{len(qvecs)} | "
                f"{statistics.fmean(recalls):.3f} |"
            )
            print(rows[-1], flush=True)
    header = (
        f"- Chunks: {n_chunks:,} in 500 containers (synthetic, clustered unit vectors, dim {dim})\n"
        f"- Hardware: {hardware()}\n"
        f"- {info[0].split(' on ')[0]}, pgvector {info[1]}, HNSW m=16 ef_construction=64 (defaults), "
        f"ef_search={HNSW_EF_SEARCH}, iterative_scan=strict_order\n"
        f"- {queries} queries per row; recall against an exact scan on the first {exact_queries}\n"
        f"- Measured {datetime.now(UTC):%Y-%m-%d}"
    )
    return header, rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--exact-queries", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    try:
        switch_database("bench")
    except EvalDatabaseError as exc:
        print(f"refusing: {exc}")
        return 2

    async def go() -> tuple[str, list[str]]:
        try:
            return await run(args.queries, args.exact_queries, args.seed)
        finally:
            await dispose_engine()

    header, rows = asyncio.run(go())
    table = (
        "| Readable | k | p50 ms | p95 ms | fewer than k | recall vs exact |\n|---|---|---|---|---|---|\n"
        + "\n".join(rows)
    )
    print(header)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            "# Vector search benchmark\n\nProduced by `scripts/synth_corpus.py` + `scripts/bench_search.py`.\n\n"
            f"{header}\n\n{table}\n",
            encoding="utf-8",
        )
        print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
