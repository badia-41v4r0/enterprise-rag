"""Prometheus metrics (R19, PLAN-v0.5 5.5), served on an internal port only (`ERAG_METRICS_PORT`).

A small registry that writes the Prometheus text format, so there is no extra dependency. Labels carry ids,
names of models, stages and outcomes: never a question, a document title or any user text.

Gauges that describe shared state (queue depths, the age of the oldest job) are computed from Postgres when
scraped, by async collectors, so every replica reports the same truth.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from threading import Lock

log = logging.getLogger(__name__)

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: str = "") -> str:
    parts = [f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = labels
        self._lock = Lock()

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self.label_names):
            raise ValueError(f"{self.name} expects labels {self.label_names}, got {tuple(labels)}")
        return tuple(str(labels[n]) for n in self.label_names)

    def header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return self.header() + [f"{self.name}{_labels(self.label_names, k)} {v:g}" for k, v in items]


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._values[self._key(labels)] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def replace(self, values: dict[tuple[str, ...], float]) -> None:
        with self._lock:
            self._values = dict(values)

    def value(self, **labels: str) -> float:
        return self._values.get(self._key(labels), 0.0)

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return self.header() + [f"{self.name}{_labels(self.label_names, k)} {v:g}" for k, v in items]


class Histogram(_Metric):
    kind = "histogram"

    def __init__(
        self, name: str, help_text: str, labels: tuple[str, ...] = (), buckets: tuple[float, ...] = DEFAULT_BUCKETS
    ) -> None:
        super().__init__(name, help_text, labels)
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * (len(self.buckets) + 1))
            for i, b in enumerate(self.buckets):
                if value <= b:
                    counts[i] += 1
            counts[-1] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value

    @contextmanager
    def time(self, **labels: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(time.perf_counter() - start, **labels)

    def count(self, **labels: str) -> int:
        counts = self._counts.get(self._key(labels))
        return counts[-1] if counts else 0

    def render(self) -> list[str]:
        out = self.header()
        with self._lock:
            items = sorted(self._counts.items())
            sums = dict(self._sums)
        for key, counts in items:
            for b, c in zip(self.buckets, counts, strict=False):
                le = "+Inf" if math.isinf(b) else f"{b:g}"
                out.append(f"{self.name}_bucket{_labels(self.label_names, key, f'le="{le}"')} {c}")
            out.append(f"{self.name}_bucket{_labels(self.label_names, key, 'le="+Inf"')} {counts[-1]}")
            out.append(f"{self.name}_sum{_labels(self.label_names, key)} {sums.get(key, 0.0):g}")
            out.append(f"{self.name}_count{_labels(self.label_names, key)} {counts[-1]}")
        return out


class Registry:
    def __init__(self) -> None:
        self.metrics: list[_Metric] = []
        self.collectors: list[Callable[[], Awaitable[None]]] = []

    def add(self, metric: _Metric) -> _Metric:
        self.metrics.append(metric)
        return metric

    def collector(self, fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        self.collectors.append(fn)
        return fn

    async def render(self) -> str:
        for fn in self.collectors:
            try:
                await asyncio.wait_for(fn(), timeout=5)
            except Exception:
                log.warning("metrics collector %s failed", getattr(fn, "__name__", fn), exc_info=True)
                COLLECTOR_ERRORS.inc(collector=getattr(fn, "__name__", "?"))
        lines: list[str] = []
        for m in self.metrics:
            lines.extend(m.render())  # type: ignore[attr-defined]
        return "\n".join(lines) + "\n"


REGISTRY = Registry()


def counter(name: str, help_text: str, labels: tuple[str, ...] = ()) -> Counter:
    m = Counter(name, help_text, labels)
    REGISTRY.add(m)
    return m


def gauge(name: str, help_text: str, labels: tuple[str, ...] = ()) -> Gauge:
    m = Gauge(name, help_text, labels)
    REGISTRY.add(m)
    return m


def histogram(name: str, help_text: str, labels: tuple[str, ...] = (), **kw: tuple[float, ...]) -> Histogram:
    m = Histogram(name, help_text, labels, **kw)
    REGISTRY.add(m)
    return m


# --- The metrics of EnterpriseRAG (reference: docs/operations/metrics.md) -----------------------------------

COLLECTOR_ERRORS = counter("erag_metrics_collector_errors_total", "Metric collectors that failed", ("collector",))

HTTP_REQUESTS = counter("erag_http_requests_total", "HTTP requests by route template and status", ("route", "status"))
HTTP_LATENCY = histogram("erag_http_request_seconds", "HTTP request latency by route template", ("route",))

STAGE_LATENCY = histogram(
    "erag_stage_seconds",
    "Latency per stage of a chat answer: embed, vector, fulltext, rerank, generation, queue_wait",
    ("stage",),
)
RERANKER_FALLBACKS = counter("erag_reranker_fallbacks_total", "Retrievals answered without the reranker")
GENERATIONS = counter("erag_generations_total", "Model generations by model and outcome", ("model", "outcome"))
QUEUE_DEPTH = gauge("erag_generation_queue_depth", "Requests waiting for a generation slot", ("pool",))
QUEUE_TIMEOUTS = counter("erag_generation_queue_timeouts_total", "Requests that gave up waiting for a slot", ("pool",))
LIMIT_REFUSALS = counter("erag_limit_refusals_total", "Requests refused by a quota or limit", ("code",))
COST_USD = counter("erag_cost_usd_total", "Model cost recorded, by provider", ("provider",))
TOKENS = counter("erag_tokens_total", "Tokens recorded, by provider and direction", ("provider", "direction"))

INGEST_JOBS = counter("erag_ingestion_jobs_total", "Ingestion jobs finished, by outcome", ("outcome",))
INGEST_SECONDS = histogram("erag_ingestion_job_seconds", "Time to index one document version")
INGEST_QUEUE = gauge("erag_ingestion_queue", "Ingestion jobs by state", ("state",))
INGEST_OLDEST = gauge("erag_ingestion_oldest_queued_seconds", "Age of the oldest job ready to run")
REAPED = counter("erag_reaped_total", "Jobs and sync runs re-queued because their worker died")

# Security signals (alert rules in deploy/monitoring/alerts.yml).
ACL_MISMATCHES = counter("erag_acl_recheck_mismatches_total", "Hits dropped by the post-search ACL recheck (expect 0)")
TOOL_DENIALS = counter("erag_tool_denials_total", "Tool calls refused", ("reason",))
LOGIN_FAILURES = counter("erag_login_failures_total", "Failed logins and second factors", ("step",))
STEP_UP_FAILURES = counter("erag_step_up_failures_total", "Failed step-up re-authentications")


@REGISTRY.collector
async def collect_shared_state() -> None:
    """Queue depths and ages from Postgres, at scrape time."""
    from sqlalchemy import func, select, text

    from app.core.db import get_sessionmaker
    from app.models import GenerationWaiter, IngestionJob

    async with get_sessionmaker()() as db:
        waiting = (await db.execute(select(GenerationWaiter.pool, func.count()).group_by(GenerationWaiter.pool))).all()
        QUEUE_DEPTH.replace({(str(p),): float(n) for p, n in waiting})
        states = (await db.execute(select(IngestionJob.state, func.count()).group_by(IngestionJob.state))).all()
        INGEST_QUEUE.replace({(s.value,): float(n) for s, n in states})
        oldest = await db.scalar(
            text(
                "SELECT EXTRACT(EPOCH FROM now() - min(created_at)) FROM ingestion_jobs "
                "WHERE state = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= now())"
            )
        )
        INGEST_OLDEST.set(float(oldest or 0.0))


# --- The internal HTTP endpoint -------------------------------------------------------------------------------


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        line = request.split(b"\r\n", 1)[0].decode(errors="replace")
        if line.startswith("GET /metrics"):
            body, status = (await REGISTRY.render()).encode(), "200 OK"
            ctype = "text/plain; version=0.0.4; charset=utf-8"
        else:
            body, status, ctype = b"not found\n", "404 Not Found", "text/plain"
        head = f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        writer.write(head.encode() + body)
        await writer.drain()
    except Exception:
        log.debug("metrics request failed", exc_info=True)
    finally:
        writer.close()


async def serve(host: str, port: int) -> asyncio.Server:
    """Start the metrics endpoint. Bind it to an internal interface; it is never routed by the proxy."""
    return await asyncio.start_server(_handle, host, port)


async def start(host: str, port: int) -> asyncio.Server | None:
    """Start the endpoint if enabled; a port already taken (several processes on one host) is logged, not fatal."""
    if not port:
        return None
    try:
        server = await serve(host, port)
    except OSError as exc:
        log.warning("metrics endpoint not started on %s:%s: %s", host, port, exc)
        return None
    log.info("metrics on http://%s:%s/metrics", host, port)
    return server
