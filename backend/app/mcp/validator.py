"""Result validator: the second barrier after the server's own scoping.

Checks shape and size, tags the classification (never lower than the tool's declared floor)
and drops instruction-like strings before they reach a model (heuristic, see the threat model).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.models import Classification

REDACTED = "[removed: possible prompt injection]"
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(rules|instructions|prompts?)",
    r"disregard\s+(all\s+)?(the\s+)?(previous|prior|above)\s+\w+",
    r"you\s+are\s+now\s+(in\s+)?\w+",
    r"(system|developer)\s+(prompt|notice|message)\s*:",
    r"(call|invoke|run|use)\s+the\s+tool\s+\w+",
    r"(call|invoke|run)\s+(the\s+)?(tool\s+|function\s+)?\w+_\w+",
    r"reveal\s+(the\s+)?[\w\s]{0,40}(salar\w*|secret\w*|passwords?|budget)",
    r"</?\s*(system|tool_result|document|context)\b[^>]*>",
]
_INJECTION = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.IGNORECASE)


@dataclass
class ValidatedResult:
    ok: bool
    content: str
    classification: Classification
    flags: list[str] = field(default_factory=list)
    size_bytes: int = 0


def _scrub(value: Any, flags: list[str], classes: list[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "classification" and isinstance(v, str) and v in Classification.__members__:
                classes.append(v)
            out[str(k)] = _scrub(v, flags, classes)
        return out
    if isinstance(value, list):
        return [_scrub(v, flags, classes) for v in value]
    if isinstance(value, str):
        if _INJECTION.search(value):
            # Drop the whole string: partial redaction can leave a working instruction behind.
            flags.append("prompt_injection")
            return REDACTED
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    flags.append("non_json_value")
    return str(value)


def validate_result(payload: Any, declared: Classification, max_bytes: int) -> ValidatedResult:
    if not isinstance(payload, dict):
        return ValidatedResult(False, json.dumps({"error": "malformed tool result"}), declared, ["malformed"])
    raw = json.dumps(payload, default=str)
    size = len(raw.encode())
    if size > max_bytes:
        return ValidatedResult(
            False,
            json.dumps({"error": f"tool result too large ({size} bytes, limit {max_bytes}); ask for less data"}),
            declared,
            ["oversized"],
            size,
        )
    flags: list[str] = []
    classes: list[str] = []
    clean = _scrub(payload, flags, classes)
    classification = Classification.max(declared, *classes)
    ok = "error" not in clean
    return ValidatedResult(ok, json.dumps(clean, ensure_ascii=False), classification, sorted(set(flags)), size)
