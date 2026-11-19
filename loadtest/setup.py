"""Prepare a load-test database: the ACME demo (indexed against the fake model server), N extra users imported
through the application's own CSV import, and a generation limit on the local provider so the fair queue is
exercised. Writes the users' credentials to a CSV for Locust.

    ERAG_DATABASE_URL=postgresql+asyncpg://erag:erag@localhost:55432/erag_load \
    ERAG_OLLAMA_BASE_URL=http://127.0.0.1:18080 \
    uv run python loadtest/setup.py --users 300 --max-concurrency 8 --out /tmp/load-users.csv

Refuses a database whose name does not contain "load": it truncates everything.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random

ROLES = [("Employee", 6), ("Engineering", 3), ("Sales", 2), ("HR", 1), ("Finance", 1)]


async def main(users: int, max_concurrency: int, out: str, no_reranker: bool) -> None:
    from sqlalchemy import update

    from app.core.config import get_settings
    from app.core.db import dispose_engine, get_sessionmaker
    from app.models import LlmProvider, Role
    from app.services.user_directory import import_csv
    from demo.acme.seed import seed

    url = get_settings().database_url
    if "load" not in url.rsplit("/", 1)[-1]:
        raise SystemExit(f"refusing to truncate {url.rsplit('/', 1)[-1]!r}: use a database named *load*")
    from sqlalchemy import text

    from app.models import Base

    async with get_sessionmaker()() as db:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await db.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await db.commit()
    await seed(password="load-test-password", ingest=True)  # noqa: S106 - demo personas of a throw-away database
    rnd = random.Random(7)
    async with get_sessionmaker()() as db:
        from sqlalchemy import select

        names = {r.name for r in (await db.scalars(select(Role))).all()}
        pool = [n for n, w in ROLES for _ in range(w) if n in names] or sorted(names)
        rows = ["email,display_name,roles"]
        for i in range(users):
            roles = {rnd.choice(pool)}
            if "Employee" in names:
                roles.add("Employee")
            rows.append(f"load{i:05d}@acme.test,Load User {i},{';'.join(sorted(roles))}")
        result = await import_csv(db, "\n".join(rows) + "\n", dry_run=False)
        if result.errors:
            raise SystemExit("import failed: " + "; ".join(result.errors[:5]))
        await db.execute(
            update(LlmProvider).where(LlmProvider.slug == "ollama").values(max_concurrency=max_concurrency)
        )
        if no_reranker:
            # The CPU reranker (about 1.3 s per search here) would make this a reranker benchmark.
            from app.rag.settings import KEY
            from app.services.settings_store import put_setting

            await put_setting(db, KEY, {"reranker": ""}, user_id=None)
        await db.commit()
    with open(out, "w", newline="") as fh:  # noqa: ASYNC230 - one small file at the end of a script
        w = csv.writer(fh)
        w.writerow(["email", "password"])
        for r in result.rows:
            w.writerow([r.email, r.temporary_password])
    await dispose_engine()
    print(f"seeded ACME, imported {result.created} users, ollama max_concurrency={max_concurrency}; credentials: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=300)
    ap.add_argument("--max-concurrency", type=int, default=8)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-reranker", action="store_true", help="retrieval without the reranker (Admin > Retrieval)")
    a = ap.parse_args()
    asyncio.run(main(a.users, a.max_concurrency, a.out, a.no_reranker))
