"""Zero-downtime upgrades need expand/contract migrations (PLAN-v0.5 5.6): during a rolling update the
previous release runs against the new schema, so `upgrade()` may add but must not drop, rename or retype
anything that release reads.

A migration that has to break the rule declares it:
- `CONTRACT = "<what is removed, and the release that stopped reading it>"` for the contract step, which ships
  at least one release after the code stopped using the object; or
- `REQUIRES_DOWNTIME = "<why>"` when there is no compatible path (the upgrade guide then says to stop the app).

Migrations before v0.5 (0001-0011) predate the rule.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[2] / "backend" / "migrations" / "versions"
FIRST_CHECKED = 12

BREAKING_OPS = {"drop_column", "drop_table", "rename_table", "drop_constraint"}
BREAKING_SQL = re.compile(
    r"\b(DROP\s+(TABLE|COLUMN)|RENAME\s+(TO|COLUMN)|ALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE|SET\s+NOT\s+NULL)\b",
    re.IGNORECASE,
)


def breaking_operations(source: str) -> list[str]:
    tree = ast.parse(source)
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    found = []
    for node in ast.walk(upgrade):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name = node.func.attr
        if name in BREAKING_OPS:
            found.append(f"op.{name}")
        elif name == "alter_column" and any(k.arg in ("new_column_name", "type_", "nullable") for k in node.keywords):
            found.append("op.alter_column(" + ",".join(k.arg or "" for k in node.keywords) + ")")
        elif name == "execute":
            # The SQL text of the call: plain strings and the literal parts of f-strings (each counted once).
            parts = [str(c.value) for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            for m in BREAKING_SQL.finditer(" ".join(parts)):
                found.append(f"SQL {m.group(0)}")
    return found


def declared(source: str) -> bool:
    return bool(re.search(r"^(CONTRACT|REQUIRES_DOWNTIME)\s*=\s*\S", source, re.MULTILINE))


def test_v05_migrations_are_expand_only_or_declare_why_not() -> None:
    problems = []
    checked = 0
    for path in sorted(VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py")):
        if int(path.name[:4]) < FIRST_CHECKED:
            continue
        checked += 1
        source = path.read_text(encoding="utf-8")
        ops = breaking_operations(source)
        if ops and not declared(source):
            problems.append(f"{path.name}: {', '.join(ops)}")
    assert checked >= 8
    assert not problems, "migrations that break the previous release without declaring it:\n" + "\n".join(problems)


def test_the_checker_catches_what_it_should() -> None:
    bad = """
def upgrade():
    op.drop_column("users", "email")
    op.alter_column("users", "name", new_column_name="full_name")
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE halfvec(1024)")
    op.execute(f"ALTER TABLE t RENAME TO {x}")

def downgrade():
    op.drop_table("anything")  # downgrades may remove what upgrade added
"""
    assert breaking_operations(bad) == [
        "op.drop_column",
        "op.alter_column(new_column_name)",
        "SQL ALTER COLUMN embedding TYPE",
        "SQL RENAME TO",
    ]
    ok = """
def upgrade():
    op.add_column("users", sa.Column("x", sa.Integer(), nullable=True))
    op.create_index("ix", "users", ["x"])
    op.execute("CREATE INDEX IF NOT EXISTS ix2 ON users (x)")
"""
    assert breaking_operations(ok) == []
    assert declared('REQUIRES_DOWNTIME = "type change"\n') and not declared("x = 1\n")
