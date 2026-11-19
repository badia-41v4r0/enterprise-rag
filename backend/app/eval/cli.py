"""`erag eval ...`: generate, prepare, run, compare, baseline, gate and embed-cache (ADR 0048)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.eval.corpus import EVAL_DIR

RUNS_DIR = EVAL_DIR / "runs"
BASELINES_DIR = EVAL_DIR / "baselines"


def _switch() -> int | None:
    from app.eval.prepare import EvalDatabaseError, switch_database

    try:
        switch_database("eval")
    except EvalDatabaseError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    return None


def _print_summary(report: dict[str, Any]) -> None:
    m = report["metrics"]
    print(f"run {report['run_id']}  set {report['config']['set']}  commit {(report.get('commit') or '?')[:10]}")
    print(f"  runs {m['runs']} ({m['answerable_runs']} answerable, {m['unanswerable_runs']} unanswerable)")
    print(
        f"  recall@5 {m['recall@5']:.3f}  recall@8 {m['recall@8']:.3f}  MRR {m['mrr']:.3f}  "
        f"nDCG@10 {m['ndcg@10']:.3f}  abstention {m['abstention']:.3f}"
    )
    lat = m["latency_ms"]
    print(
        f"  latency ms: embed p50 {lat['embed_p50']} p95 {lat['embed_p95']}, "
        f"search p50 {lat['search_p50']} p95 {lat['search_p95']}"
    )
    print(f"  leaks {m['leaks']}  reranker fallbacks {m.get('reranker_fallbacks', 0)}")
    if "answers" in m:
        a = m["answers"]
        print(
            f"  answers ({a['model']}, {a['judged']} judged, indicative): citation precision "
            f"{a['citation_precision']:.3f}  faithfulness {a['faithfulness']:.3f}"
        )
    for leak in m["leaked"][:20]:
        print(f"    LEAK {leak}")
    print("  by tag (answerable):")
    for tag, v in m["by_tag"].items():
        print(f"    {tag:<14} runs {v['runs']:>3}  recall@8 {v['recall@8']:.3f}  MRR {v['mrr']:.3f}")


def _write_report(report: dict[str, Any], path: Path | None) -> Path:
    if path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = RUNS_DIR / f"{stamp}-{report['config']['set']}{'-' + report['label'] if report['label'] else ''}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


async def _run(
    set_name: str, label: str, mode: str, overrides: list[str] | None = None, with_answers: bool = False
) -> dict[str, Any]:
    from app.core.db import dispose_engine, get_sessionmaker
    from app.eval.prepare import build_embedder
    from app.eval.runner import run_set
    from app.eval.schema import load_set

    eval_set = load_set(set_name)
    embedder = build_embedder("record" if mode == "record" else "strict")
    try:
        async with get_sessionmaker()() as db:
            from app.rag.settings import load as load_retrieval

            cfg = await load_retrieval(db)
            if overrides:
                cfg = cfg.merged(dict(o.split("=", 1) for o in overrides))
            answers = None
            if with_answers:
                from app.llm.router import build_providers

                answers = build_providers()[0]  # the local Ollama chat model; never a cloud provider
            report = await run_set(db, eval_set, embedder, label=label, retrieval=cfg, answers=answers)
    finally:
        await dispose_engine()
    if embedder.recorded:
        embedder.save()
    return report


def _cmd_generate(_: argparse.Namespace) -> int:
    from app.eval.corpus import write

    print(f"wrote {write()} files under eval/")
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    if (rc := _switch()) is not None:
        return rc
    from app.eval.prepare import prepare

    stats = asyncio.run(prepare("record" if args.live else "strict"))
    print("evaluation database ready: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if (rc := _switch()) is not None:
        return rc
    report = asyncio.run(
        _run(args.set, args.label, "record" if args.live else "strict", args.retrieval, args.with_answers)
    )
    path = _write_report(report, Path(args.json) if args.json else None)
    _print_summary(report)
    print(f"report: {path}")
    return 1 if report["metrics"]["leaks"] else 0


def _load_report(ref: str) -> dict[str, Any]:
    path = Path(ref)
    if not path.exists():
        matches = sorted(RUNS_DIR.glob(f"*{ref}*.json"))
        if not matches:
            raise SystemExit(f"no report {ref}")
        path = matches[-1]
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _cmd_compare(args: argparse.Namespace) -> int:
    from app.eval.runner import compare

    diff = compare(_load_report(args.a), _load_report(args.b))
    print(f"{'metric':<12} {'A':>8} {'B':>8} {'delta':>8}")
    for name, (va, vb, d) in diff["metrics"].items():
        print(f"{name:<12} {va:>8.3f} {vb:>8.3f} {d:>+8.3f}")
    print("\nrecall@8 by tag:")
    for tag, (va, vb) in diff["by_tag"].items():
        print(f"  {tag:<14} {va:.3f} -> {vb:.3f}")
    for title, rows in (("better", diff["better"]), ("worse", diff["worse"])):
        print(f"\n{title} ({len(rows)}):")
        for item, persona, key, before, after in rows[: args.limit]:
            print(f"  {item:<12} {persona:<24} {key} {before:.3f} -> {after:.3f}")
    return 0


def _cmd_baseline(args: argparse.Namespace) -> int:
    if (rc := _switch()) is not None:
        return rc
    report = asyncio.run(_run(args.set, "baseline", "strict"))
    _print_summary(report)
    if report["metrics"]["leaks"]:
        print("refusing to store a baseline with leaks", file=sys.stderr)
        return 1
    path = _write_report(report, BASELINES_DIR / f"{args.set}.json")
    print(f"baseline written: {path}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    if (rc := _switch()) is not None:
        return rc
    from app.eval.metrics import gate

    baseline_path = Path(args.baseline) if args.baseline else BASELINES_DIR / f"{args.set}.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    report = asyncio.run(_run(args.set, "gate", "strict"))
    path = _write_report(report, Path(args.json) if args.json else None)
    _print_summary(report)
    print(f"report: {path}")
    reasons = gate(report["metrics"], baseline["metrics"], args.max_drop)
    if reasons:
        print("EVALUATION GATE FAILED:\n  " + "\n  ".join(reasons), file=sys.stderr)
        return 1
    print(f"evaluation gate passed (baseline {baseline_path.name}, max drop {args.max_drop})")
    return 0


def _cmd_embed_cache(args: argparse.Namespace) -> int:
    if (rc := _switch()) is not None:
        return rc

    async def run() -> tuple[int, int]:
        from app.core.config import get_settings
        from app.core.db import dispose_engine
        from app.eval.prepare import build_embedder, ensure_database, load_corpus
        from app.eval.schema import load_set

        await ensure_database(get_settings().database_url)
        embedder = build_embedder("record")
        try:
            await load_corpus(embedder)
        finally:
            await dispose_engine()
        for name in args.sets:
            await embedder.embed([i.question for i in load_set(name).items])
        return embedder.recorded, embedder.save(prune=True)

    recorded, kept = asyncio.run(run())
    print(f"recorded {recorded} new embeddings; the cache now holds {kept} (unused entries removed)")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sp = sub.add_parser("eval", help="permission-aware retrieval evaluation (ADR 0048)").add_subparsers(
        dest="eval_cmd", required=True
    )
    p = sp.add_parser("generate", help="regenerate the evaluation corpus and the generated questions")
    p.set_defaults(func=_cmd_generate)
    p = sp.add_parser("prepare", help="create, migrate and load the evaluation database")
    p.add_argument("--live", action="store_true", help="embed missing texts with Ollama and record them")
    p.set_defaults(func=_cmd_prepare)
    p = sp.add_parser("run", help="run a set as each persona and write a report (exit 1 on any leak)")
    p.add_argument("--set", default="acme")
    p.add_argument("--label", default="")
    p.add_argument("--json", help="report path (default eval/runs/<time>-<set>.json)")
    p.add_argument("--live", action="store_true", help="embed missing questions with Ollama and record them")
    p.add_argument(
        "--retrieval", action="append", metavar="KEY=VALUE", help="override a retrieval setting for this run only"
    )
    p.add_argument(
        "--with-answers",
        action="store_true",
        help="also generate answers with the local chat model: citation precision and a faithfulness judge",
    )
    p.set_defaults(func=_cmd_run)
    p = sp.add_parser("compare", help="compare two reports (paths, or a run id fragment under eval/runs)")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=_cmd_compare)
    p = sp.add_parser("baseline", help="run the set and store eval/baselines/<set>.json")
    p.add_argument("--set", default="acme")
    p.set_defaults(func=_cmd_baseline)
    p = sp.add_parser("gate", help="run the set and fail on leakage or a drop against the baseline")
    p.add_argument("--set", default="acme")
    p.add_argument("--baseline")
    p.add_argument("--max-drop", type=float, default=0.02)
    p.add_argument("--json")
    p.set_defaults(func=_cmd_gate)
    p = sp.add_parser("embed-cache", help="record the real embeddings of the corpus and the questions (needs Ollama)")
    p.add_argument("--sets", nargs="+", default=["acme"])
    p.set_defaults(func=_cmd_embed_cache)
