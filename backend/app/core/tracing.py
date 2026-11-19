"""Traces (R19, PLAN-v0.5 5.5) through the OpenTelemetry API.

Spans cover a chat answer: retrieval (embed, vector, full text, rerank), the queue wait, the model call and
tools. Attributes are ids, counts, sizes, model names and outcomes, never the text of a question, a document
or an answer. Every span carries the request's correlation id (`erag.request_id`), the same as in the logs
and on the audit entry.

Without an SDK the API is a no-op. To export, install `opentelemetry-sdk` and
`opentelemetry-exporter-otlp-proto-http` and set `ERAG_OTEL_ENDPOINT` (e.g. http://collector:4318/v1/traces).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from app.core.context import current_request_id
from app.core.metrics import STAGE_LATENCY

log = logging.getLogger(__name__)
tracer = trace.get_tracer("enterprise-rag")

_ALLOWED_TYPES = (bool, int, float)


@contextmanager
def stage(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """A span named `erag.<name>` that also feeds the `erag_stage_seconds` histogram. String attributes must
    be identifiers (ids, model names, modes); text never goes here."""
    start = time.perf_counter()
    with tracer.start_as_current_span(f"erag.{name}") as span:
        rid = current_request_id()
        if rid:
            span.set_attribute("erag.request_id", rid)
        for key, value in attributes.items():
            if isinstance(value, _ALLOWED_TYPES) or (isinstance(value, str) and len(value) <= 200):
                span.set_attribute(f"erag.{key}", value)
        try:
            yield span
        finally:
            STAGE_LATENCY.observe(time.perf_counter() - start, stage=name)


def configure_tracing(endpoint: str, service_name: str) -> bool:
    """Install the SDK exporter when available and configured. Returns whether traces are exported."""
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found]
    except ImportError:
        log.warning("ERAG_OTEL_ENDPOINT is set but opentelemetry-sdk / the OTLP exporter is not installed")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    log.info("exporting traces to %s", endpoint)
    return True
