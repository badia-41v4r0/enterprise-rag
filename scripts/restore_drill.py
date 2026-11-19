"""Restore drill (R19, PLAN-v0.5 5.4): back up, restore into a throw-away database, migrate, verify, log in,
search, and compare the results with those taken before the backup.

    uv run python scripts/restore_drill.py \
        --source postgresql+asyncpg://erag:erag@localhost:55432/erag \
        --container enterprise-rag-postgres-1 --email admin@acme.test --password "$ERAG_DEMO_PASSWORD"

The dump here is a logical one (`pg_dump -Fc`), which is what the drill needs to prove: that a backup of this
release restores, migrates and serves the same answers. Point-in-time recovery itself is pgBackRest's job
(docs/operations/backup.md); run the same steps 3-6 after a pgBackRest restore with `--skip-dump --target`.

Searches use stored embeddings as query vectors, so the drill needs no model server. Nothing it prints is
document text: only ids and counts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], env: dict[str, str] | None = None, stdout: object = None) -> None:
    shown = cmd if "--password" not in cmd else [*cmd[: cmd.index("--password") + 1], "***"]
    print("+ " + " ".join(shown))
    subprocess.run(cmd, check=True, env={**os.environ, **(env or {})}, stdout=stdout, cwd=ROOT)  # noqa: S603


async def probe(email: str, password: str | None, samples: int) -> dict[str, object]:
    """Searches as `email` with stored embeddings as queries; with a password, also a real login."""
    from sqlalchemy import select

    from app.authz.policy import effective_permissions
    from app.core.db import dispose_engine, get_sessionmaker
    from app.models import Chunk, User
    from app.rag.vector_store import AccessFilter, PgVectorStore

    out: dict[str, object] = {}
    async with get_sessionmaker()() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if user is None:
            raise SystemExit(f"no user {email}")
        perms = await effective_permissions(db, user)
        rows = (await db.execute(select(Chunk.id, Chunk.embedding).order_by(Chunk.id).limit(samples))).all()
        store = PgVectorStore(db)
        searches = {}
        for cid, emb in rows:
            vec = emb.to_list() if hasattr(emb, "to_list") else list(emb)
            hits = await store.search(vec, AccessFilter.from_permissions(perms), 8)
            searches[str(cid)] = [str(h.chunk_id) for h in hits]
        out["searches"] = searches
    if password is not None:
        import httpx

        from app.main import create_app

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://drill") as c:
            r = await c.post("/api/auth/login", json={"email": email, "password": password})
            out["login_status"] = r.status_code
    await dispose_engine()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("probe")  # internal: runs against ERAG_DATABASE_URL
    p.add_argument("--email", required=True)
    p.add_argument("--password")
    p.add_argument("--samples", type=int, default=20)
    p.add_argument("--out", required=True)
    ap.add_argument("--source", help="database to back up (asyncpg URL)")
    ap.add_argument("--target-db", default="erag_restore_drill", help="throw-away database name on the same server")
    ap.add_argument("--container", help="Postgres container for pg_dump/pg_restore (else local binaries)")
    ap.add_argument("--email")
    ap.add_argument("--password", default=os.environ.get("ERAG_DRILL_PASSWORD"))
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--keep", action="store_true", help="keep the restored database")
    ap.add_argument("--blob-sample", type=int, default=50, help="blobs checked by backup verify (0: skip)")
    args = ap.parse_args()

    if args.cmd == "probe":
        result = asyncio.run(probe(args.email, args.password, args.samples))
        Path(args.out).write_text(json.dumps(result))
        return 0
    if not (args.source and args.email):
        ap.error("--source and --email are required")

    src = urlsplit(args.source)
    db_name = src.path.lstrip("/")
    user = src.username or "postgres"
    target_url = args.source.rsplit("/", 1)[0] + "/" + args.target_db
    pg = ["docker", "exec", "-i", args.container] if args.container else []
    py = [sys.executable, str(Path(__file__).resolve())]

    with tempfile.TemporaryDirectory() as tmp:
        before, after, dump = Path(tmp) / "before.json", Path(tmp) / "after.json", Path(tmp) / "erag.dump"
        print("1. results before the backup")
        run(
            [*py, "probe", "--email", args.email, "--samples", str(args.samples), "--out", str(before)],
            env={"ERAG_DATABASE_URL": args.source},
        )
        print("2. back up and restore into a throw-away database")
        with dump.open("wb") as fh:
            run([*pg, "pg_dump", "-U", user, "-Fc", db_name], stdout=fh)
        run([*pg, "dropdb", "-U", user, "--if-exists", args.target_db])
        run([*pg, "createdb", "-U", user, args.target_db])
        with dump.open("rb") as fh:
            print(f"+ pg_restore -d {args.target_db}")
            subprocess.run([*pg, "pg_restore", "-U", user, "--no-owner", "-d", args.target_db], stdin=fh, check=True)  # noqa: S603
        try:
            env = {"ERAG_DATABASE_URL": target_url}
            print("3. migrate the restored database to this release")
            run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)
            print("4. verify schema, master key, blobs and embeddings")
            run([sys.executable, "-m", "app.cli", "backup", "verify", "--sample", str(args.blob_sample)], env=env)
            print("5-6. log in and search on the restored database")
            cmd = [*py, "probe", "--email", args.email, "--samples", str(args.samples), "--out", str(after)]
            if args.password:
                cmd += ["--password", args.password]
            run(cmd, env=env)
            a, b = json.loads(before.read_text()), json.loads(after.read_text())
            same = sum(a["searches"].get(k) == v for k, v in b["searches"].items())
            total = len(a["searches"])
            print(f"   searches identical: {same}/{total}")
            if args.password:
                print(f"   login: HTTP {b.get('login_status')}")
            ok = same == total and total > 0 and (not args.password or b.get("login_status") == 200)
            print("restore drill: " + ("PASSED" if ok else "FAILED"))
            return 0 if ok else 1
        finally:
            if not args.keep:
                run([*pg, "dropdb", "-U", user, "--if-exists", args.target_db])


if __name__ == "__main__":
    sys.exit(main())
