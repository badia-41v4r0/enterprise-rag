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

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
