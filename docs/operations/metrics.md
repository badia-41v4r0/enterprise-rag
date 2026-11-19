# Metrics reference

EnterpriseRAG exposes Prometheus metrics on an **internal** listener (ADR 0052):

| Process | Address | Setting |
|---|---|---|
| app | `ERAG_METRICS_HOST:ERAG_METRICS_PORT` (default `127.0.0.1:9464`) | `ERAG_METRICS_PORT=0` turns it off |
| worker | same host, port + 1 (default 9465) | |

In Compose the listeners bind inside the project network and are not published; scrape them from a Prometheus on that network (`app:9464`, `worker:9465`, `app-2:9464`, ...). Never route them through the public proxy. Labels never contain user text: no questions, titles, emails or file names.

Alert rules: `deploy/monitoring/alerts.yml`.

## Chat and retrieval

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `erag_stage_seconds` | histogram | `stage` = embed, vector, fulltext, rerank, queue_wait, generation | Latency of each stage of an answer |
| `erag_http_requests_total` | counter | `route` (template), `status` | Requests |
| `erag_http_request_seconds` | histogram | `route` | Request latency (chat streams: until the stream ends) |
| `erag_reranker_fallbacks_total` | counter | | Retrievals answered without the reranker (model missing, failing or too slow) |
| `erag_generations_total` | counter | `model`, `outcome` = ok, tool_calls, error | Model calls |
| `erag_generation_queue_depth` | gauge | `pool` | Requests waiting for a slot (from Postgres, all replicas) |
| `erag_generation_queue_timeouts_total` | counter | `pool` | Requests that gave up waiting |
| `erag_limit_refusals_total` | counter | `code` = limit_reached, quota_exceeded, ... | Requests refused before anything was sent |
| `erag_tokens_total` | counter | `provider`, `direction` = in, out | Tokens recorded |
| `erag_cost_usd_total` | counter | `provider` | Cost recorded (cost per role: Admin > Roles > Usage by role, or `/api/admin/usage/by-role`) |

## Ingestion

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `erag_ingestion_jobs_total` | counter | `outcome` = done, retry, failed, dead | Finished attempts (worker) |
| `erag_ingestion_job_seconds` | histogram | | Time to index one version (worker) |
| `erag_ingestion_queue` | gauge | `state` | Jobs per state (from Postgres) |
| `erag_ingestion_oldest_queued_seconds` | gauge | | Age of the oldest job ready to run: the workers' health |
| `erag_reaped_total` | counter | | Jobs and sync runs re-queued because their worker died |

## Security signals

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `erag_acl_recheck_mismatches_total` | counter | | Hits the search filter returned but the Postgres recheck dropped. **Expected 0.** |
| `erag_login_failures_total` | counter | `step` = password, mfa | Failed logins and second factors |
| `erag_step_up_failures_total` | counter | | Failed re-authentications before sensitive actions |
| `erag_tool_denials_total` | counter | `reason` = rate_limit, unavailable, permission, classification, other | Refused tool calls |
| `erag_metrics_collector_errors_total` | counter | `collector` | A Postgres-backed gauge could not be read |

### ACL recheck mismatches
A non-zero value means the search-time filter and the authorization policy disagreed for some hit. The user was protected (the recheck dropped it), but the cause must be found: look up the `retrieval.acl_recheck_mismatch` audit entries (they carry the chunk ids and the correlation id) and the permission changes around that time.

## Health
- `GET /health/live`: the process answers (liveness probe).
- `GET /health/ready`: 503 when the database or the query embedding endpoint is down (readiness probe). The reranker, the age of the ingestion queue and the generation queue are reported as warnings in the body.

## Traces and logs
Every response carries `X-Request-ID`; the same id is in every log line of the request (`request_id`), on its audit entries (`correlation_id`) and on its spans (`erag.request_id`). To export traces install `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` and set `ERAG_OTEL_ENDPOINT`.
