"""The evaluation set format (`eval/sets/<name>.yaml`) and its validation against the declared structure."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.eval.corpus import SETS_DIR, Oracle, document_keys


class EvalSetError(ValueError):
    pass


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[str] = Field(default_factory=list)
    match: Literal["all", "any"] = "all"
    answerable: bool | None = None
    must_not_see: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_answerable(self) -> Expectation:
        if self.answerable is None:
            self.answerable = bool(self.documents)
        if self.answerable and not self.documents:
            raise ValueError("answerable is true but no documents are listed")
        if not self.answerable and self.documents:
            raise ValueError("answerable is false but documents are listed")
        return self


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    as_users: list[str] = Field(min_length=1)
    expect: dict[str, Expectation]
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _expect_matches_users(self) -> EvalItem:
        if set(self.expect) != set(self.as_users):
            raise ValueError(f"expect must have exactly one entry per persona in as_users ({self.id})")
        return self


class EvalSet(BaseModel):
    name: str
    items: list[EvalItem]
    digest: str  # sha256 of the files the set was loaded from, in load order

    def runs(self) -> list[tuple[EvalItem, str, Expectation]]:
        return [(item, user, item.expect[user]) for item in self.items for user in item.as_users]


def _read(path: Path, seen: set[Path], hasher: hashlib._Hash) -> list[EvalItem]:
    path = path.resolve()
    if path in seen:
        raise EvalSetError(f"include cycle at {path.name}")
    seen.add(path)
    raw = path.read_bytes()
    hasher.update(raw)
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise EvalSetError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise EvalSetError(f"{path.name}: expected a mapping with `items`")
    items: list[EvalItem] = []
    for inc in data.get("include", []) or []:
        items += _read(path.parent / inc, seen, hasher)
    for raw_item in data.get("items", []) or []:
        try:
            items.append(EvalItem.model_validate(raw_item))
        except ValueError as exc:
            ident = raw_item.get("id", "?") if isinstance(raw_item, dict) else "?"
            raise EvalSetError(f"{path.name}: item {ident}: {exc}") from exc
    return items


def validate_against_structure(items: list[EvalItem], known_documents: set[str] | None = None) -> None:
    """Every persona and document must exist, expected documents must be readable by the persona and
    `must_not_see` documents must not be (otherwise the item itself is wrong)."""
    oracle = Oracle()
    docs = document_keys() if known_documents is None else known_documents
    ids: set[str] = set()
    problems: list[str] = []
    for item in items:
        if item.id in ids:
            problems.append(f"duplicate id {item.id}")
        ids.add(item.id)
        for user, exp in item.expect.items():
            try:
                oracle.persona(user)
            except KeyError:
                problems.append(f"{item.id}: unknown persona {user}")
                continue
            for d in exp.documents + exp.must_not_see:
                if d not in docs:
                    problems.append(f"{item.id}: unknown document {d}")
            for d in exp.documents:
                if d in docs and not oracle.can_read(user, d):
                    problems.append(f"{item.id}: {user} cannot read expected document {d}")
            for d in exp.must_not_see:
                if d in docs and oracle.can_read(user, d):
                    problems.append(f"{item.id}: {user} can read must_not_see document {d}")
    if problems:
        raise EvalSetError("invalid evaluation set:\n  " + "\n  ".join(problems))


def load_set(name_or_path: str, *, validate: bool = True) -> EvalSet:
    path = Path(name_or_path)
    if not path.suffix:
        path = SETS_DIR / f"{name_or_path}.yaml"
    if not path.exists():
        raise EvalSetError(f"no evaluation set at {path}")
    hasher = hashlib.sha256()
    items = _read(path, set(), hasher)
    if validate:
        validate_against_structure(items)
    return EvalSet(name=path.stem, items=items, digest=hasher.hexdigest())
