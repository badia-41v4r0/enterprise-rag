"""Run the three ACME demo scenarios against a running instance and print what happened.

    uv run python scripts/demo_scenarios.py --base http://127.0.0.1:8000

Scenarios (docs/PLAN.md section 9):
1. Signature: "What is the salary band for a senior engineer?" as HR manager, engineer, new hire.
2. Tool: "Show me open invoices over 10k EUR." as finance analyst and engineer.
3. Revocation: the admin removes the HR role from the HR manager; the same question loses access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

PASSWORD = os.environ.get("ERAG_DEMO_PASSWORD", "acme-demo-password")
SALARY_Q = "What is the salary band for a senior engineer?"
INVOICE_Q = "Show me open invoices over 10k EUR."


class Session:
    def __init__(self, base: str, email: str) -> None:
        self.email = email
        self.c = httpx.Client(base_url=base, timeout=600)
        r = self.c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        r.raise_for_status()
        self.c.headers["X-CSRF-Token"] = r.json()["csrf_token"]

    def ask(self, question: str) -> dict[str, Any]:
        events = []
        with self.c.stream("POST", "/api/chat", json={"message": question}) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
        done = events[-1] if events and events[-1]["type"] == "done" else {}
        cites = next((e["items"] for e in events if e["type"] == "citations"), [])
        return {
            "model": done.get("model"),
            "classification": done.get("classification"),
            "sources": sorted({f"{c['title']} ({c['container']})" for c in cites}),
            "tools": [f"{e['name']} {e['arguments']}" for e in events if e["type"] == "tool_call"],
            "refusal": next((e["reason"] for e in events if e["type"] == "refusal"), None),
            "answer": done.get("answer", ""),
        }


def show(who: str, question: str, res: dict[str, Any]) -> None:
    print(f"\n  [{who}] {question}")
    print(
        f"    model: {res['model'] or 'none (no authorized context, no model called)'} | class: {res['classification']}"
    )
    print(f"    sources: {', '.join(res['sources']) or '-'}")
    if res["tools"]:
        print(f"    tools: {'; '.join(res['tools'])}")
    print(f"    answer: {res['answer'][:300]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BASE", "http://127.0.0.1:8000"))
    args = ap.parse_args()

    print("1. Signature scenario: same question, different people")
    for who in ("hr", "engineer", "newhire"):
        show(who, SALARY_Q, Session(args.base, f"{who}@acme.test").ask(SALARY_Q))

    print("\n2. Tool scenario")
    for who in ("finance", "engineer"):
        s = Session(args.base, f"{who}@acme.test")
        tools = [t["name"] for t in s.c.get("/api/tools").json()]
        print(f"\n  [{who}] tools visible to the model: {tools}")
        show(who, INVOICE_Q, s.ask(INVOICE_Q))

    print("\n3. Revocation scenario")
    admin = Session(args.base, "admin@acme.test")
    users = {u["email"]: u for u in admin.c.get("/api/admin/users").json()}
    roles = {r["name"]: r["id"] for r in admin.c.get("/api/admin/roles").json()}
    hr = users["hr@acme.test"]
    original = hr["role_ids"]
    try:
        admin.c.patch(f"/api/admin/users/{hr['id']}", json={"role_ids": [roles["Employee"]]}).raise_for_status()
        print("  admin removed the HR role from hr@acme.test")
        show("hr (after revocation)", SALARY_Q, Session(args.base, "hr@acme.test").ask(SALARY_Q))
    finally:
        admin.c.patch(f"/api/admin/users/{hr['id']}", json={"role_ids": original}).raise_for_status()
        print("\n  HR role restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
