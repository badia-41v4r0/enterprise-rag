"""Command line entry point: `python -m app.cli <command>` (or `erag <command>`)."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys


def _cmd_create_admin(args: argparse.Namespace) -> int:
    from app.services.users import create_admin

    password = args.password or os.environ.get("ERAG_ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    if len(password) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        return 2
    created = asyncio.run(create_admin(args.email, password))
    print(f"Admin {'created' if created else 'already exists'}: {args.email}")
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from app.ingestion.worker import run_worker

    asyncio.run(run_worker(once=args.once))
    return 0


def _cmd_seed_acme(args: argparse.Namespace) -> int:
    from demo.acme.seed import seed

    asyncio.run(seed(password=args.password, ingest=not args.no_ingest))
    return 0


def _cmd_secrets_init(args: argparse.Namespace) -> int:
    from pathlib import Path

    from app.core.crypto import generate_key, write_key_file

    key = generate_key()
    if args.file:
        try:
            write_key_file(Path(args.file), key)
        except FileExistsError:
            print(f"{args.file} already exists; refusing to overwrite a master key.", file=sys.stderr)
            return 1
        print(f"Master key written to {args.file} (mode 0600). Set ERAG_SECRETS_KEY_FILE={args.file}.")
    else:
        print(key)
    return 0


def _cmd_secrets_set(args: argparse.Namespace) -> int:
    from pathlib import Path

    if args.from_env:
        value = os.environ.get(args.from_env, "")
    elif args.from_file:
        value = Path(args.from_file).read_text().strip()
    else:
        value = getpass.getpass(f"Value for {args.name}: ")
    if not value:
        print("Empty value; nothing stored.", file=sys.stderr)
        return 2

    async def run() -> None:
        from app.core.db import get_sessionmaker
        from app.services import vault

        async with get_sessionmaker()() as db:
            row = await vault.set_secret(db, args.name, value, user_id=None)
            await db.commit()
            print(f"Stored secret {args.name} (version {row.version}). The value is not shown.")

    asyncio.run(run())
    return 0


def _cmd_secrets_list(_: argparse.Namespace) -> int:
    async def run() -> None:
        from sqlalchemy import select

        from app.core.db import get_sessionmaker
        from app.models import Secret

        async with get_sessionmaker()() as db:
            for r in (await db.scalars(select(Secret).order_by(Secret.name))).all():
                print(f"{r.name}\tv{r.version}\tupdated {r.updated_at:%Y-%m-%d %H:%M}\tlast used {r.last_used_at}")

    asyncio.run(run())
    return 0


def _cmd_secrets_rotate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from app.core.crypto import Cipher, decode_key, get_cipher

    new = Cipher(decode_key(Path(args.new_key_file).read_text()))

    async def run() -> None:
        from app.core.db import get_sessionmaker
        from app.services.vault import rotate_master

        async with get_sessionmaker()() as db:
            res = await rotate_master(db, get_cipher(), new)
            await db.commit()
            print(
                f"Re-encrypted {res.secrets} secrets and {res.mfa} MFA secrets with key {new.key_id}. "
                f"Now point ERAG_SECRETS_KEY_FILE at {args.new_key_file} and restart."
            )

    asyncio.run(run())
    return 0


def _cmd_reset_mfa(args: argparse.Namespace) -> int:
    async def run() -> bool:
        from sqlalchemy import delete, select

        from app.core.db import get_sessionmaker
        from app.models import Session, User
        from app.services import mfa
        from app.services.audit import audit

        async with get_sessionmaker()() as db:
            user = await db.scalar(select(User).where(User.email == args.email.lower()))
            if user is None:
                return False
            await mfa.reset(db, user)
            await db.execute(delete(Session).where(Session.user_id == user.id))
            audit(db, "admin.user.reset_mfa_cli", resource=f"user:{user.id}")
            await db.commit()
            return True

    ok = asyncio.run(run())
    print(f"MFA reset for {args.email}" if ok else f"No user {args.email}")
    return 0 if ok else 1


def _ensure_dev_key_file() -> None:
    """Dev demo: create the master key file on first start if it is configured but missing."""
    from app.core.config import get_settings
    from app.core.crypto import generate_key, write_key_file

    s = get_settings()
    if s.secrets_key is None and s.secrets_key_file is not None and not s.secrets_key_file.exists() and not s.is_prod:
        write_key_file(s.secrets_key_file, generate_key())
        print(f"Generated a new credential-store master key at {s.secrets_key_file}")


async def _ensure_builtin_providers() -> None:
    """Built-in `ollama` and `openrouter` providers from the environment (idempotent, ADR 0023)."""
    from app.core.db import dispose_engine, get_sessionmaker
    from app.llm.registry import ensure_builtin_providers

    async with get_sessionmaker()() as db:
        await ensure_builtin_providers(db)
    await dispose_engine()
    print("Built-in LLM providers ensured (ollama, openrouter).")


def _cmd_bootstrap(_: argparse.Namespace) -> int:
    """Container start-up hook: seed the ACME demo when ERAG_SEED_DEMO=true (documents go to the worker)."""
    _ensure_dev_key_file()
    asyncio.run(_ensure_builtin_providers())
    if os.environ.get("ERAG_SEED_DEMO", "false").lower() != "true":
        return 0
    from demo.acme.seed import seed

    asyncio.run(seed(password=os.environ.get("ERAG_DEMO_PASSWORD", "acme-demo-password"), ingest=False))
    return 0


def _cmd_reindex(args: argparse.Namespace) -> int:
    async def run() -> int:
        from app.core.db import dispose_engine, get_sessionmaker
        from app.services.reindex import queue_reindex

        try:
            async with get_sessionmaker()() as db:
                return await queue_reindex(db, container_slug=args.container, stale_only=not args.all)
        finally:
            await dispose_engine()

    n = asyncio.run(run())
    print(f"queued {n} document version(s) for re-indexing; the worker processes them in the background")
    return 0


def _cmd_maintenance(_: argparse.Namespace) -> int:
    async def run() -> int:
        from app.core.db import dispose_engine
        from app.services.maintenance import run_once

        try:
            r = await run_once()
        finally:
            await dispose_engine()
        if not r.ran:
            print("another process is running maintenance right now; nothing done")
            return 1
        print(f"deleted {r.sessions_deleted} session(s) and {r.conversations_deleted} conversation(s)")
        return 0

    return asyncio.run(run())


def _cmd_backup_verify(args: argparse.Namespace) -> int:
    import json

    async def run() -> dict[str, object]:
        from app.core.db import dispose_engine, get_sessionmaker
        from app.ingestion.blobstore import get_blobstore
        from app.services.audit import audit
        from app.services.backup import verify

        try:
            async with get_sessionmaker()() as db:
                report = await verify(db, get_blobstore(), sample=args.sample, full=args.full)
                audit(
                    db,
                    "backup.verify",
                    decision="allow" if report.ok else "error",
                    details={c.name: c.ok for c in report.checks},
                )
                await db.commit()
                return report.as_dict()
        finally:
            await dispose_engine()

    result = asyncio.run(run())
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for c in result["checks"]:  # type: ignore[attr-defined]
            print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']:<11} {c['detail']}")
        print("backup verify: " + ("passed" if result["ok"] else "FAILED"))
    return 0 if result["ok"] else 1


def _cmd_blobs_migrate(_: argparse.Namespace) -> int:
    from app.core.config import get_settings
    from app.ingestion.blobstore import LocalBlobStore, migrate_blobs
    from app.ingestion.s3 import S3BlobStore

    s = get_settings()
    target = S3BlobStore(
        s.s3_endpoint,
        s.s3_bucket,
        s.s3_access_key_id,
        s.s3_secret_access_key,
        region=s.s3_region,
        prefix=s.s3_prefix,
        sse=s.s3_sse,
    )
    copied, present = asyncio.run(migrate_blobs(LocalBlobStore(s.blob_dir), target))
    print(f"copied {copied} blob(s), {present} already in the bucket; now set ERAG_BLOB_BACKEND=s3 and restart")
    return 0


def _cmd_models_list(_: argparse.Namespace) -> int:
    from app.ml.models import MODELS, installed, models_dir

    print(f"model directory: {models_dir()}")
    for m in MODELS.values():
        state = "installed" if installed(m.name) else "not installed"
        print(f"  {m.name:<20} {m.kind:<9} {m.size_mb:>5} MB  {m.license:<12.12}  {state}  {m.description}")
    return 0


def _cmd_models_install(args: argparse.Namespace) -> int:
    from pathlib import Path

    from app.ml.models import MODELS, ModelError, install

    spec = MODELS.get(args.name)
    if spec is None:
        print(f"unknown model {args.name}; see `erag models list`", file=sys.stderr)
        return 2
    print(f"{spec.name}: {spec.repo} at {spec.revision[:12]}, {spec.size_mb} MB, license {spec.license}")
    try:
        path = install(args.name, source_dir=Path(args.from_dir) if args.from_dir else None)
    except (ModelError, OSError) as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    print(f"installed and verified (SHA-256) in {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="erag")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-admin", help="create the first administrator")
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="defaults to $ERAG_ADMIN_PASSWORD or an interactive prompt")
    p.set_defaults(func=_cmd_create_admin)

    p = sub.add_parser("worker", help="run the ingestion worker")
    p.add_argument("--once", action="store_true", help="process the queue once and exit")
    p.set_defaults(func=_cmd_worker)

    p = sub.add_parser("seed-acme", help="seed the ACME Corp demo (users, roles, containers, docs, tools)")
    p.add_argument("--password", default=os.environ.get("ERAG_DEMO_PASSWORD", "acme-demo-password"))
    p.add_argument("--no-ingest", action="store_true", help="queue documents but leave them to the worker")
    p.set_defaults(func=_cmd_seed_acme)

    p = sub.add_parser("reset-mfa", help="remove a user's second factor (lost device) and end their sessions")
    p.add_argument("--email", required=True)
    p.set_defaults(func=_cmd_reset_mfa)

    sp = sub.add_parser("secrets", help="credential store").add_subparsers(dest="secrets_cmd", required=True)
    p = sp.add_parser("init", help="generate a master key (printed, or written to --file with mode 0600)")
    p.add_argument("--file")
    p.set_defaults(func=_cmd_secrets_init)
    p = sp.add_parser("set", help="store a secret (value from --from-env, --from-file or a prompt; never printed)")
    p.add_argument("name")
    p.add_argument("--from-env", metavar="VAR")
    p.add_argument("--from-file", metavar="PATH")
    p.set_defaults(func=_cmd_secrets_set)
    p = sp.add_parser("list", help="list secret names and metadata (never values)")
    p.set_defaults(func=_cmd_secrets_list)
    p = sp.add_parser("rotate-master", help="re-encrypt everything with a new master key")
    p.add_argument("--new-key-file", required=True)
    p.set_defaults(func=_cmd_secrets_rotate)

    p = sub.add_parser("bootstrap", help="start-up hook (seeds the demo when ERAG_SEED_DEMO=true)")
    p.set_defaults(func=_cmd_bootstrap)

    p = sub.add_parser("reindex", help="re-index current document versions in the background (no downtime)")
    p.add_argument("--container", help="only this container (slug)")
    p.add_argument("--all", action="store_true", help="every document, not only those made by an older chunker")
    p.set_defaults(func=_cmd_reindex)

    p = sub.add_parser("maintenance", help="delete expired/idle sessions and apply conversation retention now")
    p.set_defaults(func=_cmd_maintenance)

    sp = sub.add_parser("backup", help="backup checks").add_subparsers(dest="backup_cmd", required=True)
    p = sp.add_parser(
        "verify", help="check schema, master key, blobs and embeddings of this installation (e.g. after a restore)"
    )
    p.add_argument("--sample", type=int, default=50, help="current versions whose blob is checked (default 50)")
    p.add_argument("--full", action="store_true", help="check every blob")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.set_defaults(func=_cmd_backup_verify)

    sp = sub.add_parser("blobs", help="blob storage").add_subparsers(dest="blobs_cmd", required=True)
    p = sp.add_parser("migrate", help="copy local blobs to S3 (configured with ERAG_S3_*), verifying each hash")
    p.add_argument("--to", choices=["s3"], required=True)
    p.set_defaults(func=_cmd_blobs_migrate)

    sp = sub.add_parser("models", help="local model files (reranker, tokenizer)").add_subparsers(
        dest="models_cmd", required=True
    )
    p = sp.add_parser("list", help="known models and whether they are installed")
    p.set_defaults(func=_cmd_models_list)
    p = sp.add_parser("install", help="download a pinned model (or copy it with --from-dir) and verify its hash")
    p.add_argument("name")
    p.add_argument("--from-dir", help="a directory holding the files, for offline installations")
    p.set_defaults(func=_cmd_models_install)

    from app.eval.cli import register as register_eval

    register_eval(sub)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
