"""The evaluation set format, its validation against the declared structure, the generated corpus and the
recorded embeddings. None of these need a database or a model."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.eval.corpus import EVAL_CONTAINERS, Oracle, document_keys, generate, render
from app.eval.embed_cache import CachedEmbedder, EmbeddingCacheMiss
from app.eval.prepare import cache_path
from app.eval.schema import EvalSetError, load_set


def test_acme_set_loads_and_is_consistent_with_the_structure() -> None:
    s = load_set("acme")
    assert len(s.items) >= 140
    assert len(s.runs()) > len(s.items)  # several personas per item
    assert any(not exp.answerable for _, _, exp in s.runs())
    assert any(exp.must_not_see for _, _, exp in s.runs())


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "t.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "items:\n- {id: a, question: q, as_users: [nobody@acme.test], expect: {nobody@acme.test: {}}}\n",
            "unknown persona",
        ),
        (
            "items:\n- {id: a, question: q, as_users: [newhire@acme.test],"
            " expect: {newhire@acme.test: {documents: [finance/nope.md]}}}\n",
            "unknown document",
        ),
        (
            "items:\n- {id: a, question: q, as_users: [newhire@acme.test],"
            " expect: {newhire@acme.test: {documents: [finance/q3-2026-budget.md]}}}\n",
            "cannot read expected document",
        ),
        (
            "items:\n- {id: a, question: q, as_users: [hr@acme.test],"
            " expect: {hr@acme.test: {answerable: false, must_not_see: [hr-confidential/salary-bands-2026.md]}}}\n",
            "can read must_not_see document",
        ),
        (
            "items:\n- {id: a, question: q, as_users: [hr@acme.test, exec@acme.test], expect: {hr@acme.test: {}}}\n",
            "exactly one entry per persona",
        ),
        (
            "items:\n- {id: a, question: q, as_users: [hr@acme.test], expect: {hr@acme.test: {answerable: true}}}\n",
            "no documents are listed",
        ),
        ("items: [unclosed\n", "invalid YAML"),
    ],
)
def test_invalid_sets_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(EvalSetError, match=message):
        load_set(_write(tmp_path, body))


def test_generated_corpus_matches_the_committed_files() -> None:
    files = render(generate())
    assert len(files) > 250
    changed = [str(p) for p, text in files.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
    assert changed == [], "run `erag eval generate` and commit the result"


def test_oracle_follows_the_declared_structure() -> None:
    o = Oracle()
    assert o.can_read("sales@acme.test", "sales-contracts/ct-2025-0001.md")
    assert not o.can_read("newhire@acme.test", "sales-contracts/ct-2025-0001.md")
    assert o.can_read("exec@acme.test", "sales-contracts/ct-2025-0001.md")
    # Container read is not enough for a restricted document.
    assert not o.can_read("exec@acme.test", "hr-confidential/investigation-2026-07.md")
    assert o.can_read("hr@acme.test", "hr-confidential/investigation-2026-07.md")
    assert not o.can_read("hr.assistant@acme.test", "hr-confidential/settlement-2026-05.md")
    assert {c.slug for c in EVAL_CONTAINERS} >= {"sales-contracts", "support-kb", "product-catalog"}
    assert "product-catalog/ax-1001.md" in document_keys()


def test_recorded_embeddings_cover_every_question() -> None:
    s = get_settings()
    cache = CachedEmbedder(cache_path(), s.embed_model, s.embed_dim)
    questions = [i.question for i in load_set("acme").items]
    vectors = asyncio.run(cache.embed(questions))
    assert len(vectors) == len(questions) and all(len(v) == s.embed_dim for v in vectors)


def test_cache_round_trip_and_strict_miss(tmp_path: Path) -> None:
    s = get_settings()
    src = CachedEmbedder(cache_path(), s.embed_model, s.embed_dim)
    some = next(iter(src.vectors.items()))
    path = tmp_path / "c.bin"
    copy = CachedEmbedder(path, s.embed_model, s.embed_dim)
    copy.vectors[some[0]] = some[1]
    assert copy.save() == 1
    again = CachedEmbedder(path, s.embed_model, s.embed_dim)
    assert again.vectors == {some[0]: some[1]}
    with pytest.raises(EmbeddingCacheMiss, match="erag eval embed-cache"):
        asyncio.run(again.embed(["a text nobody recorded"]))
    with pytest.raises(ValueError, match="holds"):
        CachedEmbedder(path, "other-model", s.embed_dim)
