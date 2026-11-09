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

    async def run() -> tuple[bool, bool]:
        from sqlalchemy import select

        from app.core.db import dispose_engine, get_sessionmaker
        from app.models import User
        from app.services import setup

        created = await create_admin(args.email, password)
        completed = False
        async with get_sessionmaker()() as db:
            row = await setup.get_state(db)
            if row is not None and row.completed_at is None:
                # A headless installation: the administrator was created on the server, so the wizard is not
                # needed any more (ADR 0034). Its token is destroyed.
                user = await db.scalar(select(User).where(User.email == args.email.lower()))
                await setup.complete_existing(db, "cli", user.id if user else None)
                completed = True
        await dispose_engine()
        return created, completed

    created, completed = asyncio.run(run())
    print(f"Admin {'created' if created else 'already exists'}: {args.email}")
    if completed:
        print("Setup marked as completed (the setup wizard and its token are disabled).")
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


async def _setup_on_start() -> None:
    """Setup mode for a fresh installation; an existing one is recorded as completed (ADR 0034)."""
    from app.core.config import get_settings
    from app.core.db import dispose_engine, get_sessionmaker
    from app.services import setup

    s = get_settings()
    async with get_sessionmaker()() as db:
        res = await setup.init_on_start(db, s)
    await dispose_engine()
    if res.mode != "setup":
        return
    port = os.environ.get("APP_PORT", "8000")
    print("=" * 72)
    print("EnterpriseRAG is in SETUP MODE: open the web interface to run the setup wizard.")
    print(f"  URL: http://127.0.0.1:{port}/  (or this server's address)")
    if res.new_token:
        print(f"  Setup token: {res.new_token}")
        print(f"  (also in {s.setup_token_path}, readable only by the application user)")
    elif res.token_from_init:
        print("  Setup token: the one printed by `erag init`.")
    else:
        print(f"  Setup token: unchanged since the last start (see {s.setup_token_path} or the earlier log).")
    print("  Lost it? Run `erag setup --new-token` on the server.")
    print("=" * 72, flush=True)


def _cmd_bootstrap(_: argparse.Namespace) -> int:
    """Container start-up hook: built-in providers, the ACME demo when ERAG_SEED_DEMO=true (development and
    scripts/demo_scenarios.py; ADR 0040), then setup mode for a fresh installation."""
    from app.core.config import get_settings

    _ensure_dev_key_file()
    asyncio.run(_ensure_builtin_providers())
    s = get_settings()
    if s.seed_demo:
        if s.is_prod:
            # The demo's admin persona has a published password: never in production (ADR 0040).
            print("ERAG_SEED_DEMO=true is ignored in production; choose the demo in the setup wizard instead.")
        else:
            asyncio.run(_seed_and_complete(s.demo_password.get_secret_value()))
            return 0
    asyncio.run(_setup_on_start())
    return 0


async def _seed_and_complete(password: str) -> None:
    """One event loop for the seed and the completion record (the engine's pool is bound to its loop)."""
    from app.core.db import dispose_engine, get_sessionmaker
    from app.services import setup
    from demo.acme.seed import seed

    await seed(password=password, ingest=False)
    async with get_sessionmaker()() as db:
        row = await setup.get_state(db)
        if row is None or row.completed_at is None:
            await setup.complete_existing(db, "seed_demo")
            print("Demo seeded with its admin persona (ERAG_SEED_DEMO=true): setup counts as completed.")
    await dispose_engine()


def _cmd_setup(args: argparse.Namespace) -> int:
    """Server-side setup commands. They need the database and the data volume, never a web request."""

    async def run() -> int:
        from app.core.config import get_settings
        from app.core.db import dispose_engine, get_sessionmaker
        from app.services import setup

        s = get_settings()
        async with get_sessionmaker()() as db:
            row = await setup.get_state(db)
            if args.status or not (args.reset or args.new_token):
                if row is None:
                    print("No setup record (development database, or never started): not in setup mode.")
                elif row.completed_at is None:
                    print(
                        f"Setup mode: step {setup.current_step(row)} of {len(setup.STEPS) - 1}; "
                        f"token expires {row.token_expires_at:%Y-%m-%d %H:%M} UTC."
                    )
                else:
                    print(f"Setup completed {row.completed_at:%Y-%m-%d %H:%M} UTC (via {row.completed_via}).")
                await dispose_engine()
                return 0
            if args.reset and not args.yes:
                print(
                    "This re-opens the setup wizard: until it is finished again, only administrators can use the "
                    "application. Re-run with --yes to confirm.",
                    file=sys.stderr,
                )
                await dispose_engine()
                return 2
            try:
                token = await setup.new_token_cli(db, s, reopen=bool(args.reset))
            except setup.SetupError as exc:
                print(str(exc), file=sys.stderr)
                await dispose_engine()
                return 1
        await dispose_engine()
        print("Setup re-opened." if args.reset else "New setup token issued (the previous one no longer works).")
        print(f"Setup token: {token}")
        print(f"(also in {s.setup_token_path}). Restart the app if it does not pick this up within 30 seconds.")
        return 0

    return asyncio.run(run())


def _cmd_init(args: argparse.Namespace) -> int:
    from app.installer import run_init

    return run_init(args)


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

    p = sub.add_parser("bootstrap", help="start-up hook (setup mode for a fresh installation; demo seed in dev)")
    p.set_defaults(func=_cmd_bootstrap)

    p = sub.add_parser("setup", help="setup mode on the server: --status, --new-token, --reset")
    p.add_argument("--status", action="store_true", help="show whether setup is running or completed")
    p.add_argument("--new-token", action="store_true", help="issue a new setup token (setup mode only)")
    p.add_argument("--reset", action="store_true", help="re-open a completed setup (prints a new token)")
    p.add_argument("--yes", action="store_true", help="confirm --reset")
    p.set_defaults(func=_cmd_setup)

    from app.installer import add_init_arguments

    p = sub.add_parser("init", help="prepare a production environment file, master key and setup token")
    add_init_arguments(p)
    p.set_defaults(func=_cmd_init)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
