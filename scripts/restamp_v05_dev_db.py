"""Bring a development database migrated from the v0.5 branch *before* it was integrated with v0.4 onto the
current migration history. Released installations (v0.4 and earlier) do not need this.

Before the integration, v0.5's migrations were numbered 0012-0019; v0.4 (released) owns 0012 (`setup_state`),
so v0.5's became 0013-0020. A database stamped by the old branch at 00NN therefore has v0.5's schema up to
old 00NN, no `setup_state` table, and a stamp that now means something else. This script:

1. checks that this is such a database (stamp 0012-0019 and no `setup_state` table), else does nothing;
2. applies v0.4's migration 0012 (it creates `setup_state` and records setup as completed when users exist);
3. re-stamps the database at the new number of the same v0.5 migration (old 00NN -> new 00NN+1).

Then `alembic upgrade head` continues normally.

    ERAG_DATABASE_URL=postgresql+asyncpg://... uv run python scripts/restamp_v05_dev_db.py          # dry run
    ERAG_DATABASE_URL=postgresql+asyncpg://... uv run python scripts/restamp_v05_dev_db.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path

from sqlalchemy import text

VERSIONS = Path(__file__).resolve().parents[1] / "backend" / "migrations" / "versions"
OLD_V05 = {f"{n:04d}" for n in range(12, 20)}


def load_v04_setup_migration():  # type: ignore[no-untyped-def]
    path = next(VERSIONS.glob("0012_*.py"))
    spec = importlib.util.spec_from_file_location("migration_0012", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main(apply: bool) -> int:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.core.db import dispose_engine, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        stamp = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        has_setup = await conn.scalar(text("SELECT to_regclass('public.setup_state') IS NOT NULL"))
        if stamp not in OLD_V05 or has_setup:
            print(f"nothing to do: stamped {stamp}, setup_state {'present' if has_setup else 'absent'}")
            await dispose_engine()
            return 0
        new = f"{int(stamp) + 1:04d}"
        print(f"pre-integration v0.5 database at old {stamp}: apply v0.4's 0012 (setup_state), then stamp {new}")
        if not apply:
            print("dry run: re-run with --apply")
            await dispose_engine()
            return 0
        migration = load_v04_setup_migration()

        def run_v04_0012(sync_conn):  # type: ignore[no-untyped-def]
            with Operations.context(MigrationContext.configure(sync_conn)):
                migration.upgrade()

        await conn.run_sync(run_v04_0012)
        await conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": new})
    await dispose_engine()
    print(f"done: stamped {new}; now run `alembic upgrade head`")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(asyncio.run(main(ap.parse_args().apply)))
