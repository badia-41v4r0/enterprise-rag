"""Load a synthetic corpus into the benchmark database, to measure vector search latency at scale.

    uv run python scripts/synth_corpus.py --chunks 1000000

Writes to ERAG_BENCH_DATABASE_URL (default: the app's server, database `erag_bench`) and refuses to touch the
application database. Vectors are synthetic (clustered, unit length): index latency and filter behaviour do not
depend on semantic quality, and embedding millions of chunks with a real model would take days.

Structure, deterministic from --seed: 500 containers named bench-c000..bench-c499, documents of 5 to 15 chunks
spread over them, 1 percent of documents restricted. scripts/bench_search.py derives the permission patterns
(users who can read 90, 10 and 1 percent of the containers) from the container order.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import time
import uuid

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector

from app.core.config import get_settings
from app.eval.prepare import EvalDatabaseError, ensure_database, switch_database

N_CONTAINERS = 500
N_CLUSTERS = 256


def dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def load(n_chunks: int, seed: int, batch: int, work_mem: str) -> None:
    url = get_settings().database_url
    await ensure_database(url)
    rng = np.random.default_rng(seed)
    dim = get_settings().embed_dim
    conn = await asyncpg.connect(dsn(url))
    await register_vector(conn)
    try:
        t0 = time.perf_counter()
        await conn.execute(
            "TRUNCATE chunks, document_versions, documents, grants, user_roles, roles, containers CASCADE"
        )
        await conn.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
        containers = [(uuid.uuid4(), f"Bench {i:03d}", f"bench-c{i:03d}") for i in range(N_CONTAINERS)]
        await conn.copy_records_to_table(
            "containers",
            records=[(cid, name, slug, "", "internal") for cid, name, slug in containers],
            columns=["id", "name", "slug", "description", "classification"],
        )
        centroids = rng.standard_normal((N_CLUSTERS, dim)).astype(np.float32)
        written, doc_no = 0, 0
        while written < n_chunks:
            docs, versions, chunk_rows = [], [], []
            while len(chunk_rows) < batch and written + len(chunk_rows) < n_chunks:
                doc_id, ver_id = uuid.uuid4(), uuid.uuid4()
                cid = containers[int(rng.integers(N_CONTAINERS))][0]
                restricted = bool(rng.random() < 0.01)
                n = min(int(rng.integers(5, 16)), n_chunks - written - len(chunk_rows))
                centre = centroids[int(rng.integers(N_CLUSTERS))]
                vecs = centre + 0.35 * rng.standard_normal((n, dim)).astype(np.float32)
                vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
                sha = hashlib.sha256(doc_id.bytes).hexdigest()
                docs.append((doc_id, cid, f"Bench document {doc_no}", f"bench-{doc_no}.md", restricted))
                versions.append((ver_id, doc_id, sha, f"bench/{sha}", "text/markdown", 0, "ready"))
                for o in range(n):
                    chunk_rows.append(
                        (uuid.uuid4(), ver_id, doc_id, cid, o, f"bench chunk {doc_no}/{o}", 250, vecs[o], "{}")
                    )
                doc_no += 1
            await conn.copy_records_to_table(
                "documents", records=docs, columns=["id", "container_id", "title", "filename", "restricted"]
            )
            await conn.copy_records_to_table(
                "document_versions",
                records=versions,
                columns=["id", "document_id", "sha256", "blob_key", "mime_type", "size_bytes", "status"],
            )
            await conn.execute(
                "UPDATE documents d SET current_version_id = v.id FROM document_versions v "
                "WHERE v.document_id = d.id AND d.current_version_id IS NULL"
            )
            await conn.copy_records_to_table(
                "chunks",
                records=chunk_rows,
                columns=[
                    "id",
                    "version_id",
                    "document_id",
                    "container_id",
                    "ordinal",
                    "text",
                    "token_count",
                    "embedding",
                    "metadata",
                ],
            )
            written += len(chunk_rows)
            print(f"  {written:>10,} chunks  ({time.perf_counter() - t0:,.0f} s)", flush=True)
        print("building the HNSW index (this is the slow part)...", flush=True)
        t1 = time.perf_counter()
        # The HNSW graph must fit in maintenance_work_mem, or pgvector falls back to a much slower build
        # (about 4.5 KB per 1024-dimension vector: 1M chunks need roughly 5 GB).
        await conn.execute(f"SET maintenance_work_mem = '{work_mem}'")
        # Serial build: a parallel build allocates maintenance_work_mem in /dev/shm, which Docker caps at 64 MB.
        await conn.execute("SET max_parallel_maintenance_workers = 0")
        await conn.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
        await conn.execute("ANALYZE")
        print(
            f"done: {written:,} chunks in {doc_no:,} documents; load {t1 - t0:,.0f} s, "
            f"index {time.perf_counter() - t1:,.0f} s"
        )
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chunks", type=int, default=1_000_000)
    ap.add_argument("--seed", type=int, default=20261118)
    ap.add_argument("--batch", type=int, default=20_000)
    ap.add_argument("--maintenance-work-mem", default="384MB", help="Postgres memory for the index build")
    args = ap.parse_args()
    try:
        switch_database("bench")
    except EvalDatabaseError as exc:
        print(f"refusing: {exc}")
        return 2
    asyncio.run(load(args.chunks, args.seed, args.batch, args.maintenance_work_mem))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
