# 0044. Observability without content

**Status:** Accepted (2026-11-19).

## Decision
- **Metrics** in Prometheus text format from a small built-in registry (no new dependency), served by a separate listener on `ERAG_METRICS_PORT` (worker: port + 1), never through the public app port or the proxy. Shared state (queue depths, the age of the oldest ready job) is read from Postgres at scrape time, so every replica reports the same truth.
- **What is measured:** latency per stage (embed, vector, full text, rerank, queue wait, generation), HTTP by route **template**, generations by model and outcome, queue depth and timeouts, reranker fallbacks, quota and limit refusals by code, tokens and cost by provider, ingestion outcomes, durations, states and reaped work.
- **Security signals as metrics with alerts:** ACL recheck mismatches (expected 0, critical), failed logins and second factors, step-up failures, tool denials by bounded reason category. Rules in `deploy/monitoring/alerts.yml`; reference in `docs/operations/metrics.md`. We ship rules, not a dashboard tool (Grafana is AGPL, D8).
- **Labels never carry user text**: no questions, titles, emails, filenames, free-form errors. Tool denial reasons are mapped to a fixed set of categories. A test checks a scrape for the text it just sent.
- **Correlation id.** Each request gets an id (a caller's `X-Request-ID` is kept only if it is a plain token of at most 64 characters). It is returned in the response, added to every log line written while serving it, stored on its audit entries (`audit_log.correlation_id`) and set on its trace spans.
- **Traces** through the OpenTelemetry API: spans for the retrieval stages, the model call and tools, with ids, counts and model names only. A no-op unless `opentelemetry-sdk` and an OTLP exporter are installed and `ERAG_OTEL_ENDPOINT` is set.
- **Health:** `/health/live` (process) and `/health/ready` (503 when the database or the query embedding endpoint is down; reranker, ingestion queue age and generation queue reported as warnings, because restarting the replica would not fix them).

## Consequences
- Operators can see load, failures and abuse without the monitoring stack ever holding company content.
- The metrics port must be kept off any public network (Compose binds it inside the project network without publishing it).
