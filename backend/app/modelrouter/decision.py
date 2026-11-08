"""Router answers, their strict validation, the deterministic rules decider and lane matching (ADR 0029).

Every decider answers the same four questions (PLAN-v0.3 section 5): task type (choice), difficulty (score
1 to 5), needs tools (yes/no) and needs long context (yes/no), each with a confidence. Whatever a decider
returns is validated against a strict schema before it is used; anything else is a decider failure and the
next decider (finally the rules) decides. The answers only select a lane: the lane's targets are filtered by
the permitted set, so no answer can widen what a request may use.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any

from app.models import Classification

TASK_TYPES: dict[str, str] = {
    "question_answering": "Answer a question from documents or general knowledge.",
    "summarization": "Summarize or condense text.",
    "extraction": "Extract specific fields, lists or tables from text.",
    "code": "Write, explain or fix source code, queries or configuration.",
    "tool_use": "Look something up live in a business system or create or change a record (needs a tool).",
    "chit_chat": "Greeting, thanks or small talk that needs no information.",
}
DIFFICULTY_LEVELS = [
    "1: trivial, a short factual answer",
    "2: easy, one fact from one source",
    "3: moderate, combine a few facts or follow a procedure",
    "4: hard, reasoning over several sources or careful writing",
    "5: very hard, multi-step analysis, complex code or calculation",
]


class DeciderError(Exception):
    """A decider failed, timed out or returned something that does not match the schema."""


@dataclass(frozen=True)
class Meta:
    """What a decider may know besides the question (never the retrieved chunks, unless local + see_context)."""

    classification: Classification
    tools_offered: bool
    context_tokens: int

    def describe(self) -> str:
        return (
            f"classification={self.classification.value}; tools_offered={'yes' if self.tools_offered else 'no'}; "
            f"retrieved_context_tokens={self.context_tokens}"
        )


@dataclass(frozen=True)
class Answers:
    task_type: str
    task_confidence: float
    difficulty: float  # 1.0 to 5.0
    difficulty_confidence: float
    needs_tools: bool
    needs_tools_p: float
    needs_long_context: bool
    needs_long_context_p: float

    def to_json(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _prob(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise DeciderError(f"{name} must be a number")
    v = float(value)
    if not 0.0 <= v <= 1.0:
        raise DeciderError(f"{name} must be between 0 and 1")
    return v


def answers_from(
    task_type: Any,
    task_confidence: Any,
    difficulty: Any,
    difficulty_confidence: Any,
    needs_tools_p: Any,
    needs_long_context_p: Any,
) -> Answers:
    """Strictly validated answers (raises DeciderError)."""
    if not isinstance(task_type, str) or task_type not in TASK_TYPES:
        raise DeciderError("task_type is not one of the allowed choices")
    if isinstance(difficulty, bool) or not isinstance(difficulty, int | float) or not math.isfinite(float(difficulty)):
        raise DeciderError("difficulty must be a number")
    d = float(difficulty)
    if not 1.0 <= d <= 5.0:
        raise DeciderError("difficulty must be between 1 and 5")
    tools = _prob(needs_tools_p, "needs_tools")
    long_ctx = _prob(needs_long_context_p, "needs_long_context")
    return Answers(
        task_type=task_type,
        task_confidence=_prob(task_confidence, "task_confidence"),
        difficulty=d,
        difficulty_confidence=_prob(difficulty_confidence, "difficulty_confidence"),
        needs_tools=tools >= 0.5,
        needs_tools_p=tools,
        needs_long_context=long_ctx >= 0.5,
        needs_long_context_p=long_ctx,
    )


# --- The rules decider (deterministic, always available, always last) -----------------------------------

_TOOL = re.compile(
    r"\b(list|show|find|search|look up|lookup|open|create|book|convert|check|status of|how many open)\b", re.I
)
_SUMMARY = re.compile(r"\b(summari[sz]e|summary|tl;?dr|overview of|in short)\b", re.I)
_EXTRACT = re.compile(r"\b(extract|list all|table of|which (?:fields|columns)|enumerate)\b", re.I)
_CODE = re.compile(r"```|\b(code|function|class|regex|sql|stack ?trace|exception|bug|compile|script|python)\b", re.I)
_CHAT = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening))\b[\s!.,]*$", re.I)


def rules_decide(question: str, meta: Meta, long_context_tokens: int = 2000) -> Answers:
    """Deterministic answers from the question's shape and the metadata only."""
    words = len(question.split())
    if _CHAT.match(question) and words <= 6:
        task = "chit_chat"
    elif meta.tools_offered and _TOOL.search(question):
        task = "tool_use"
    elif _SUMMARY.search(question):
        task = "summarization"
    elif _CODE.search(question):
        task = "code"
    elif _EXTRACT.search(question):
        task = "extraction"
    else:
        task = "question_answering"
    difficulty = 1
    if words > 12:
        difficulty += 1
    if words > 40:
        difficulty += 1
    if meta.context_tokens > 1500:
        difficulty += 1
    if task in ("code", "extraction", "summarization"):
        difficulty += 1
    if task == "chit_chat":
        difficulty = 1
    long_ctx = meta.context_tokens >= long_context_tokens
    tools = task == "tool_use"
    return Answers(
        task_type=task,
        task_confidence=1.0,
        difficulty=float(min(5, difficulty)),
        difficulty_confidence=1.0,
        needs_tools=tools,
        needs_tools_p=1.0 if tools else 0.0,
        needs_long_context=long_ctx,
        needs_long_context_p=1.0 if long_ctx else 0.0,
    )


# --- Lanes -----------------------------------------------------------------------------------------------

CONDITION_KEYS = {
    "task_types",
    "difficulty_min",
    "difficulty_max",
    "needs_tools",
    "needs_long_context",
    "classifications",
}


def validate_conditions(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise lane conditions (raises ValueError). Missing keys mean "any"."""
    unknown = set(raw) - CONDITION_KEYS
    if unknown:
        raise ValueError(f"unknown lane condition(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    tasks = raw.get("task_types") or []
    if not isinstance(tasks, list) or any(t not in TASK_TYPES for t in tasks):
        raise ValueError(f"task_types must be a list of: {', '.join(TASK_TYPES)}")
    if tasks:
        out["task_types"] = list(dict.fromkeys(tasks))
    for key in ("difficulty_min", "difficulty_max"):
        v = raw.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int | float) or not 1 <= float(v) <= 5:
            raise ValueError(f"{key} must be between 1 and 5")
        out[key] = float(v)
    if out.get("difficulty_min", 1) > out.get("difficulty_max", 5):
        raise ValueError("difficulty_min is above difficulty_max")
    for key in ("needs_tools", "needs_long_context"):
        v = raw.get(key)
        if v is None:
            continue
        if not isinstance(v, bool):
            raise ValueError(f"{key} must be true, false or absent")
        out[key] = v
    classes = raw.get("classifications") or []
    if not isinstance(classes, list) or any(c not in {x.value for x in Classification} for c in classes):
        raise ValueError("classifications must be a list of public, internal, confidential")
    if classes:
        out["classifications"] = list(dict.fromkeys(classes))
    return out


def lane_matches(conditions: dict[str, Any], answers: Answers, classification: Classification) -> bool:
    c = conditions
    if c.get("task_types") and answers.task_type not in c["task_types"]:
        return False
    if "difficulty_min" in c and answers.difficulty < float(c["difficulty_min"]):
        return False
    if "difficulty_max" in c and answers.difficulty > float(c["difficulty_max"]):
        return False
    if "needs_tools" in c and answers.needs_tools != c["needs_tools"]:
        return False
    if "needs_long_context" in c and answers.needs_long_context != c["needs_long_context"]:
        return False
    return not (c.get("classifications") and classification.value not in c["classifications"])
