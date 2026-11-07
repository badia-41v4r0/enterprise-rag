"""Fail if any installed Python dependency carries a copyleft license (GPL/AGPL/LGPL).

EnterpriseRAG is PolyForm Noncommercial and dual-licensed commercially, so only permissive
dependencies are allowed (see docs/PLAN.md section 14).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

COPYLEFT = re.compile(r"\b(A?GPL|LGPL|GNU (Affero|Lesser|General)|EUPL|SSPL|CC-BY-SA)\b", re.IGNORECASE)
# Packages whose metadata is ambiguous but that were checked by hand.
REVIEWED: dict[str, str] = {}
IGNORE = {"enterprise-rag"}


def main() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-system"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    bad = []
    for pkg in json.loads(out):
        name, lic = pkg["Name"], pkg["License"]
        if name in IGNORE or name in REVIEWED:
            continue
        if COPYLEFT.search(lic) and not re.search(r"\b(MIT|BSD|Apache)\b", lic):
            bad.append(f"{name}=={pkg['Version']}: {lic}")
        if lic.strip().upper() in {"UNKNOWN", ""}:
            print(f"warning: unknown license for {name}=={pkg['Version']}")
    if bad:
        print("Copyleft dependencies found:\n  " + "\n  ".join(bad))
        return 1
    print("License check passed: no copyleft dependencies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
