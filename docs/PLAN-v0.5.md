# EnterpriseRAG v0.5: many users in one company, hybrid search with a reranker and an evaluation set, scale and operations

> Planning document. No code yet. Builds on v0.3 (providers, quotas, model router) and can be developed in parallel with v0.4 (first-run setup). The user CSV import of section 3.4 is written as a service that the v0.4 wizard reuses.
> Status: revised after the owner's decisions (2026-11-18), section 12.

## 1. The problem

EnterpriseRAG works for a demo company with a handful of personas and a few dozen documents. A real company has **thousands of employees** using it at the same time and a corpus of **hundreds of thousands of documents**. Several things break, or would break, at that size:

- **People.** The admin user list loads every user at once (`GET /api/admin/users`). The audit log has no filters. Permissions are recomputed from the database on every request. Expired sessions are never cleaned up.
- **Concurrency.** Rate limiters live in the memory of one process (`backend/app/core/ratelimit.py`), so a second app replica doubles every limit. Budgets are checked and not reserved (ADR 0026), so concurrent requests overshoot them. One local Ollama answers one generation at a time, with no queue and no fairness between users.
- **Answer quality.** Retrieval is vector-only (`backend/app/rag/vector_store.py`). It misses exact names, contract numbers, product codes and acronyms, which are exactly what employees search for. The cut-off is a fixed cosine score (0.45), and there is **no way to measure** whether retrieval is good or whether a change made it worse.
- **Operations.** One app container and one worker. Embeddings are computed in batches of 16, in series. Blobs live on the local disk. There are no metrics, no documented backup or restore, and only Docker Compose.

## 2. Goals

1. **Many users:** admin screens that work with 10,000 users, permissions that stay fast and are revoked immediately, and fair access to models under load.
2. **Better retrieval, measured:** hybrid search (vector + full text), a local reranker, and a **permission-aware evaluation set** that compares configurations and blocks regressions in CI.
3. **Scale and operations:**
   - horizontal scaling of the app and the workers;
   - ingestion of a large corpus in reasonable time;
   - S3-compatible blob storage;
   - a vector-store path past 20 million chunks;
   - backups with a tested restore;
   - metrics, traces and alerts;
   - a Helm chart with high-availability Postgres and zero-downtime upgrades.

All of this keeps the six principles in the README. In particular, **both search legs and the reranker apply the same authorization filter**, and the reranker runs inside the application because it reads confidential text.

### Reference size and design principle

| | Benchmark target |
|---|---|
| Named users | 10,000 |
| Users active at the same time | 1,000 |
| Peak questions | 10 per second (retrieval); generation depends on the model capacity bought |
| Documents / chunks | 1 million / 20 million |
| Retrieval latency (vector + full text + rerank, p95) | under 1.5 s, excluding generation |
| New documents per day | 50,000 |

These numbers are **what the benchmarks measure, not a ceiling**. The design must grow past them by adding replicas, not by redesigning:

- **No state needed for correctness lives in a process's memory.** Limits, reservations, generation slots, the scheduler lock and permission-cache invalidation are all in Postgres. Caches only speed things up and are invalidated from Postgres.
- **Every part scales on its own:**
  - app replicas, each with its own reranker;
  - workers;
  - model endpoints, in pools per model;
  - the vector store (pgvector, or Qdrant behind the same interface).
- **Scaling is measured, not assumed:** the load test of section 7 is repeated with twice the replicas, and throughput must grow roughly in proportion.

### Non-goals

- Single sign-on (OIDC/SAML), SCIM provisioning and IdP groups. Moved to the ROADMAP.
- Multi-tenancy (several companies in one installation).
- Mirroring source-system ACLs and new connectors (SharePoint, Drive).
- OCR, LLM-based query rewriting and deny rules.

## 3. Many users in one company

### 3.1 Permission resolution at scale
- **Cache with invalidation:**
  - An `authz_state.epoch` counter in Postgres is increased **in the same transaction** as any change to grants, roles, memberships, restricted documents or user status.
  - Every app replica keeps effective permissions in memory, keyed by (user, epoch).
  - A request reads the epoch (one row) and recomputes only if it changed. Revocation is therefore immediate on every replica.
- **Selective users.** Someone who can read 1% of the corpus makes a filtered HNSW scan walk a lot of the graph before it finds k allowed rows. The search picks its plan from an estimate of how many chunks the user can read (container sizes are kept as counters):
  - above a threshold, the HNSW scan with `iterative_scan`, as today;
  - below it, an exact scan restricted to the user's containers.
  The threshold comes from the benchmark in section 7. The existing post-filter recheck stays.
- **Restricted documents:** today their ids are passed as an `IN (...)` list. For users with many of them, the filter joins the grants table instead.

### 3.2 Fair use under load
All shared state lives in **Postgres** (decision 3, section 12). It sits behind a `LimitStore` interface, so another backend (Valkey) can be added later without changing the callers.

- **Shared rate limits:** chat, login, MFA and tool limits use an UNLOGGED `rate_limit_counters` table. Each check is one atomic `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` on an autocommit connection, so it holds no locks inside the request's transaction. UNLOGGED means that a database crash resets the counters, which is acceptable for rate limits.
- **Budget reservations** replace the checks, closing ADR 0026's open item. This state is logged, because it is money.
  - Before sending, a request reserves its worst case on every applicable budget and pool in one transaction (`SELECT ... FOR UPDATE`, in a fixed order to avoid deadlocks).
  - After the stream, it settles at the real cost and releases the rest.
  - Reservations whose request died expire after a timeout.
  - The tokens of a request that fails mid-stream are recorded.
- **Generation queue with fairness, across replicas:**
  - Each model endpoint has a maximum number of concurrent generations. They are represented as **leases** in `generation_leases`, with an expiry and a heartbeat, so a dead replica gives its slots back.
  - Requests above the limit wait in `generation_waiters`. A free slot goes to the user with the fewest requests in flight and, among equals, the oldest waiter. One person running a script cannot starve the others.
  - A role can have a priority (for example a support team).
  - Waiting replicas are woken with `LISTEN/NOTIFY`.
  - The chat shows "waiting, position N". Requests time out with a clear message, and the queue depth is a metric.

### 3.3 Model serving for many users
- **Endpoint pools:** a model in the provider registry can have **several endpoints** (several Ollama or vLLM replicas, for example one per GPU), with health checks. The endpoint with the fewest requests in flight is chosen.
- All endpoints of a pool must have the same locality and clearance. The egress guard (ADR 0025) checks each one.
- **Query embeddings take priority over bulk ingestion.** They use their own endpoint or a priority lane, so a large connector sync does not slow down every chat.
- A **capacity guide** in the documentation covers concurrent generations per GPU for the recommended models and how to size them from the number of active users.

### 3.4 Admin screens that work with 10,000 users
- **Users:**
  - server-side cursor pagination;
  - search by name or email;
  - filters by role, status and last login (`users.last_login_at`);
  - bulk actions: assign or remove a role, deactivate;
  - CSV import and export. Import is a service that the v0.4 wizard reuses.
- **Audit log:** cursor pagination and filters by user, action, resource and date.
- **Usage reports** per role (requests, tokens, cost), exportable.
- **Sessions:**
  - a configurable idle timeout and absolute lifetime;
  - "sign out everywhere" for a user;
  - periodic deletion of expired sessions.
- **Conversation retention:** a configurable period after which conversations are deleted (default: keep). Deletions are audited.

## 4. Hybrid search, reranker and evaluation

### 4.1 Pipeline
```
question
 ├─ vector leg:    embedding → HNSW (authorization filter)         → top 50
 └─ full-text leg: tsquery   → GIN  (same authorization filter)    → top 50
        → reciprocal rank fusion → ACL recheck (as today) → reranker (in process) → relevance threshold → build_context
```
- **One authorization filter, two legs.** The `WHERE` clause that `AccessFilter` produces today becomes **one function used by both queries**. A test checks that they cannot diverge (section 9).
- The recheck against the current documents table runs on the fused set **before** the reranker reads any text.
- The fusion weights, k of each leg, the final k and whether the reranker is on are **runtime settings** in Admin > Retrieval. They are recorded with every answer, so answers can be compared with an evaluation run.

### 4.2 Full-text leg
- A generated `tsvector` column on `chunks` with a GIN index. It is built from the chunk text plus the document title and section heading, with the title weighted higher.
- **Language:** documents in Spanish and English. Each document gets a language at ingestion, detected or set by its container.
  - The column uses that language's dictionary (stemming) **plus** a `simple` part without stemming, so codes, numbers and names match exactly.
  - `unaccent` is applied, so "credito" finds "crédito".
- The query is parsed with `websearch_to_tsquery`, so quotes and OR work as users expect. Ranking uses `ts_rank_cd`.
- **Optional `pg_trgm`** for partial identifiers (`INV-2024-00`). It stays off unless the evaluation shows a gain.

### 4.3 Fusion
**Reciprocal rank fusion** (k = 60), with per-leg weights of 1.0 / 1.0 by default. It needs no score normalization, which matters because cosine and `ts_rank_cd` are on different scales. Weights are tuned with the evaluation set, not by intuition.

### 4.4 Reranker, inside the application process
- A **cross-encoder** that scores (question, chunk) pairs. It runs **inside every app replica** with **ONNX Runtime** (MIT) and **`tokenizers`** (Apache-2.0). There is no separate service, and adding replicas adds reranking capacity.
- **Candidate models**, compared on the evaluation set. The one with the best balance of quality and latency becomes the default; the other remains selectable.
  - `BAAI/bge-reranker-v2-m3`: Apache-2.0, multilingual, the same family as the `bge-m3` embeddings.
  - A small multilingual cross-encoder (`mmarco-mMiniLMv2-L12-H384`, Apache-2.0), about 5 times smaller.
- **Execution:**
  - an int8-quantized model loaded at start-up;
  - inference in a thread pool so that it never blocks the event loop (ONNX Runtime releases the GIL);
  - a semaphore per process that caps how many rerank batches run at the same time;
  - CPU by default; the CUDA execution provider is optional when the app replica has a GPU.
  - The app's memory limit rises from 1 GB to about 3 GB with the large model.
- **Weights are never downloaded automatically.** `erag reranker install` fetches a pinned revision and verifies its SHA-256, or takes a local path for offline installations. The image can also include the model through a build argument. Until a model is installed, the pipeline runs without the reranker and says so in Admin > Health.
- **Budget:** at most 50 pairs per question (configurable), with chunks truncated to the model's maximum length. The evaluation records the real latency on CPU and GPU.
- **Degraded mode:** if the reranker is missing, overloaded (the semaphore wait times out) or fails, the answer uses the fused order and is flagged. A metric and a warning in Admin > Health make the fallback visible. The request does not fail.
- **Relevance threshold:** the fixed cosine cut-off (`ERAG_RETRIEVAL_MIN_SCORE=0.45`) becomes a reranker-score threshold calibrated on the evaluation set. The goal is that "the documents do not answer this" says so instead of passing weak chunks to the model.
- Because it runs in process, the reranker never sends text over the network, so no locality check is needed for it.

### 4.5 Chunking improvements (they feed the evaluation)
- Token counts with the real `bge-m3` tokenizer (the same `tokenizers` library) instead of characters / 4. This makes the context budget and the reranker truncation accurate.
- Tables (DOCX, HTML, XLSX) are kept whole, or split by rows with the header repeated. XLSX rows become "column: value" text.
- The title and section path are stored on every chunk. The full-text leg uses them and citations show them.
- Changing the chunker requires re-indexing. `erag reindex` re-processes the current versions in the background, container by container, without downtime: new chunks are written beside the old ones (`chunker_version`) and swapped per document.

### 4.6 Evaluation set
**Format.** Versioned files in `eval/sets/<name>.yaml`:
```yaml
- id: acme-fin-012
  question: "¿Cuál es el plazo de pago del contrato con Iberdata?"
  as_users: [ana@acme.test, luis@acme.test]   # run once per persona
  expect:
    ana@acme.test:  { documents: ["Contrato Iberdata 2025"], answerable: true }
    luis@acme.test: { documents: [], answerable: false, must_not_see: ["Contrato Iberdata 2025"] }
  tags: [exact-name, finance]
```
- **Permission-aware by design.** Each question runs **as one or more personas**, and the expected result depends on who asks. `must_not_see` turns every question into a leakage test as well.
- **Question types**, tagged so metrics can be split by type: exact identifiers, paraphrases, acronyms, multi-document, Spanish and English, unanswerable, forbidden.
- **Sets:**
  - an `acme` set built with the demo (about 150 questions), kept in the repo and used in CI;
  - **the company's own set**, stored in the database and never in the repo (see 4.8).

**Metrics.**
| Level | Metric |
|---|---|
| Retrieval | recall@k and MRR at document level, nDCG@10, hit rate per tag |
| Safety | **leakage = 0** (any `must_not_see` in the retrieved or cited set fails the run, whatever the other numbers) |
| Answer | citation correctness (every cited chunk is in the expected documents), correct refusal on unanswerable questions, faithfulness judged by a **local** model (indicative only, never a gate) |
| Cost | latency p50/p95 per stage, tokens per answer |

**Runs.**
- `erag eval run --set acme --config hybrid-rerank` stores a run with a snapshot of the configuration (embedding model, legs, weights, reranker model, thresholds, chunker version) and the commit.
- `erag eval compare <run> <run>` lists the questions that got better or worse.
- A default **ablation** compares vector-only, full-text only, hybrid, and hybrid + reranker.

**CI gate.** On the `acme` set, leakage must be 0, and recall@8 and MRR must not drop more than 2 points from the stored baseline. Updating the baseline is a deliberate commit.

### 4.7 Evaluation in the admin area
**Admin > Evaluation** lets an administrator:
- upload or edit a set;
- run it against the current configuration or a candidate one (for example "reranker on" before turning it on for everyone);
- see the comparison per question and per tag;
- apply the candidate configuration if it is better.

Runs as personas use those personas' real permissions and are audited as evaluation runs.

### 4.8 Feedback from users
- Every answer gets thumbs up or down, with an optional comment.
- A thumbs down can be **proposed as an evaluation item**. It enters a review queue for an administrator who has read access to the cited sources, and becomes an item only after that person writes down the expected documents.
- Questions can contain sensitive data. The review queue and the company's sets are stored with a classification (default `confidential`), are visible only to administrators with the corresponding access, and are never sent to a cloud judge.

## 5. Scale and operations

### 5.1 Horizontal scaling of the app
- **Stateless app replicas.** Sessions are already in Postgres. What remains in process moves out or becomes safe:
  - rate limiters and tool limiters move to the shared store (3.2);
  - budget checks become reservations (3.2);
  - the permission cache is invalidated by epoch (3.1).
- **Singleton tasks** run on one process at a time, elected with a **Postgres advisory lock**: the connector scheduler and the cleanup of sessions, reservations, leases and stuck jobs.
- **Connections:** a configurable SQLAlchemy pool per replica plus **PgBouncer** in transaction mode.
  - `SET LOCAL hnsw.*` is compatible because it lives inside the transaction.
  - asyncpg prepared statements need PgBouncer ≥ 1.21 with prepared-statement support, or the asyncpg statement cache turned off. The chosen setting is tested.
  - `LISTEN/NOTIFY` (3.2) uses one direct connection per replica, not PgBouncer.
- **Long requests:** chat streams (SSE) go through the proxy with timeouts set for them. A replica drains its streams before stopping (graceful shutdown).

### 5.2 Ingestion of a large corpus
- **Several workers** (the queue already uses `FOR UPDATE SKIP LOCKED`), each with configurable concurrency.
- **Priorities:** an interactive upload goes before a bulk connector sync.
- **Embeddings in parallel:** larger batches and several requests in flight to the embedding endpoint pool (3.3).
- **Failure handling:**
  - retries with exponential backoff (`next_attempt_at`);
  - a **dead-letter** state, visible in the Uploads and Connectors screens with a Retry button;
  - a **reaper:** jobs and sync runs carry a heartbeat (`locked_until`) and return to the queue when their worker dies. This closes the ROADMAP item.
- **Back-pressure:** connectors stop enqueueing when the queue is above a threshold. `ERAG_CONNECTOR_MAX_ITEMS` stops being a hard limit of 1,000 and becomes a per-connector setting.
- **Large files** are parsed streaming from the blob store, with a memory cap per job.
- **Throughput target:** 50,000 documents per day on one GPU host, measured with the synthetic corpus (section 7).

### 5.3 Storage
- **S3-compatible `BlobStore`** (AWS S3, Azure through an S3 gateway, Ceph, SeaweedFS):
  - private bucket, server-side encryption, objects named by SHA-256 (dedupe as today);
  - `erag blobs migrate --to s3` moves an existing installation;
  - tests run against **SeaweedFS** (Apache-2.0). MinIO is AGPL and excluded by decision D8.
- **pgvector to 20 million chunks:**
  - embeddings stored as **`halfvec`**: half the size, with recall confirmed on the evaluation set;
  - HNSW parameters (`m`, `ef_construction`, `ef_search`) tuned with the benchmark;
  - parallel index builds with a documented `maintenance_work_mem`.
- **Beyond that:** the `VectorStore` interface gets a **Qdrant** adapter (Apache-2.0).
  - Chunks carry only their location as payload (`container_id`, `document_id`, `version_id`, `restricted`), never ACLs (ADR 0002).
  - Writes go through an outbox table, so Postgres and Qdrant cannot drift apart.
  - The recheck against Postgres stays. The full-text leg stays in Postgres.
  - It is built in v0.5 only if pgvector misses the target in the benchmark (decision 4).

### 5.4 Backup and recovery
- **Postgres:** continuous WAL archiving and point-in-time recovery with **pgBackRest** (MIT). Base backups every night, encrypted.
- **Blobs:** a versioned bucket, or for local storage a scheduled sync to a second location.
- **Master key:** backed up separately from the database (as the README already says), with a check that the backup decrypts a known value.
- **Targets:** RPO 15 minutes and RTO 4 hours (decision 6). Embeddings are included in the backup because regenerating 20 million of them takes days.
- `erag backup verify` and a **documented restore drill**, also run in CI on a throw-away installation: restore, migrate, log in, ask a question, compare with the answer from before the backup.

### 5.5 Observability
- **Metrics** in Prometheus format, on an internal port and never public:
  - latency per stage (embed, vector, full text, rerank, generation);
  - queue depth and wait time per model;
  - ingestion throughput, failures and dead letters;
  - reranker fallbacks and semaphore wait;
  - quota refusals, and cost per role and provider.
- **Security signals as metrics, with alerts:** ACL recheck mismatches (expected to be 0), denied tool calls, failed logins, step-up failures.
- **Traces** with OpenTelemetry across the request (retrieval, rerank, model, tools). Spans carry ids and sizes, **never question or document text**.
- Logs are already JSON. They gain a correlation id shared with the traces and the audit entry.
- **Health:** separate liveness and readiness. Readiness covers the database, the embedding endpoint, the reranker model being loaded, worker heartbeats and the age of the queue.
- We ship **alert rules** and a **metrics reference**, not a dashboard tool (Grafana is AGPL, D8). Companies connect their own monitoring.

### 5.6 Deployment
- **Helm chart:**
  - an app Deployment with a horizontal autoscaler (the reranker scales with it);
  - a worker Deployment and PgBouncer;
  - migrations as a pre-upgrade Job;
  - secrets from Kubernetes Secrets or an external secret store.
- **Postgres with high availability:** a managed service, or **CloudNativePG** (Apache-2.0) with a replica and automatic failover. The app reconnects after a failover.
- **Zero-downtime upgrades:**
  - an **expand/contract** rule for migrations: a release never drops or renames something the previous release reads;
  - a test runs the previous release's suite against the new schema;
  - rolling updates with readiness gates.
- **The MCP runner** needs a container engine socket. On Kubernetes it runs on a dedicated node or VM outside the cluster, as documented. Running it natively on Kubernetes is out of scope.
- **Docker Compose stays the single-host path**, with a `scale` profile: two app replicas, two workers and PgBouncer.

## 6. Data model and API (sketch)

**Users, sessions and permissions**
- `users.last_login_at`.
- `authz_state` (singleton: `epoch`).
- Container chunk counters, used by the search plan of 3.1.
- Session settings in `app_settings`.

**Shared limits and generation queue**
- `rate_limit_counters` (UNLOGGED).
- `budget_reservations` (request id, scope, amount, expires at).
- `generation_leases` (endpoint, slot, holder, expires at).
- `generation_waiters` (user, endpoint pool, priority, enqueued at).
- `llm_endpoints` (several per provider: URL, weight, health, maximum concurrency, locality as of the last check).

**Retrieval and evaluation**
- `chunks`: `tsv` (generated), `lang`, `embedding` as `halfvec`, `chunker_version`.
- `retrieval_settings` in `app_settings`.
- `eval_sets`, `eval_items`, `eval_runs` (configuration snapshot, commit, metrics), `eval_results` (per item and persona).
- `answer_feedback`.

**Ingestion**
- `ingestion_jobs`: `priority`, `next_attempt_at`, `locked_until`, and a new `dead` state.
- The same heartbeat on `sync_runs`.

**API**
- Paginated users: `/api/admin/users?cursor=&q=&role=&status=`.
- `/api/admin/users/bulk`, `/api/admin/users/import`, `/api/admin/users/export`.
- Paginated audit log: `/api/admin/audit?cursor=&user=&action=&from=&to=`.
- `/api/admin/retrieval`, `/api/admin/eval/*`, `/api/chat/feedback`.
- `/metrics` (internal port), `/health/live`, `/health/ready`.

## 7. Benchmarks (part of the work, not after it)

- **Synthetic corpus generator:**
  - 1 million documents / 20 million chunks with realistic sizes and 500 containers;
  - permission patterns: users who see 90%, 10% and 1% of the corpus, plus restricted documents.
  - Used for search latency, the HNSW versus exact-scan threshold (3.1), `halfvec` recall, reranker latency per model, and the pgvector versus Qdrant decision.
- **Load tests with Locust** (MIT; k6 is AGPL):
  - 1,000 simulated users with persona mixes, chat, uploads and admin screens;
  - reported: p95 per stage, error rate, queue wait, and proof that no limit is exceeded with several replicas;
  - **repeated with twice the app replicas and workers**, to check that throughput grows roughly in proportion (section 2).
- Results go into `docs/benchmarks/`, with the hardware used.

## 8. Security notes

- **The full-text leg is a new path to the data.** It uses the shared filter function, the same recheck and the leakage suite (section 9). Highlighting and snippets come only from chunks that passed both.
- **The reranker sees confidential text.** Running in process, it adds no new network path. Model files are pinned by hash and installed only by an explicit command.
- **Permission cache:** invalidated in the same transaction as the change, so revocation is never delayed. A test changes a grant in one replica and asks in another.
- **Shared limits in Postgres:** rate-limit counters are UNLOGGED. A database crash resets them, an accepted trade-off. Budget reservations are logged, because they are money.
- **Metrics, traces and logs** contain no question or document text. Evaluation sets and feedback are classified and stored like documents.
- **Backups** are encrypted. The restore drill also checks that restored secrets decrypt only with the master key.
- **PgBouncer** runs in transaction mode, so nothing may rely on session-level state (only `SET LOCAL`). `LISTEN` uses a direct connection.

## 9. Tests (real components, no fakes, ADR 0008)

- **Leakage suite over the hybrid path:**
  - every existing leakage test runs with full text, fusion and reranker on;
  - a property test generates random users and queries over the demo plus synthetic permissions, and checks that every chunk from **each leg** satisfies the policy, before and after fusion;
  - a query that only the full-text leg can answer ("an exact contract number in a confidential container") must return nothing for an unauthorized user.
- **Filter parity:** the vector and full-text queries are built from the same filter function, and a test fails if either one is built without it.
- **Reranker:** the real ONNX model, with tests skipped when it is not installed (like the Ollama tests). The degraded mode is tested by removing the model and by saturating the semaphore.
- **Several replicas:** rate limits, budgets and generation leases hold with two app processes; reservations and leases expire; the scheduler runs once; a grant revoked in one process takes effect in the other.
- **Ingestion:** a worker is killed mid-job and the reaper re-queues the job; backoff; dead letters; priority over a bulk sync.
- **Upgrades:** the previous release's test suite runs against the new schema (expand/contract).
- **Restore drill** in CI on a throw-away installation.
- **Evaluation gate** on the `acme` set in CI.

## 10. Documentation

**ADRs:**
| ADR | Topic |
|---|---|
| 0034 | Permission cache by epoch and the search plan for selective users |
| 0035 | Shared limits in Postgres behind `LimitStore` |
| 0036 | Budget reservations |
| 0037 | Endpoint pools and the fair generation queue |
| 0038 | Hybrid search and rank fusion |
| 0039 | Reranker inside the application process |
| 0040 | Permission-aware evaluation |
| 0041 | Horizontal scaling: PgBouncer and advisory locks |
| 0042 | S3 blob store and the vector-store scaling path |
| 0043 | Backup and recovery |
| 0044 | Observability without content |
| 0045 | Helm and zero-downtime upgrades |

**README:** updated at every release, with a full pass at the end:
- the status line and "What it does";
- Quick start, including `erag reranker install`;
- new sections "Retrieval and evaluation", "Fair use and capacity" and "Operations: scaling, metrics and backups";
- every new `ERAG_*` setting in the configuration reference;
- "Production settings";
- "Known boundaries": remove "Budgets are checked, not reserved" and add the UNLOGGED-counter trade-off;
- "Development / Useful commands", including the eval, reindex, reranker, blobs and backup commands.

**Guides:**
- an operations guide: sizing, scaling, backup and restore, upgrades, alerts;
- an evaluation guide: how a company builds its own set.

**Other documents:** `ROADMAP.md` (remove what is done; add SSO, SCIM, IdP groups and a Valkey `LimitStore`), `docs/architecture.md`, `docs/threat-model.md`, and the white paper where relevant.

## 11. Releases and effort

Evaluation comes first: retrieval should not change before it can be measured.

| Release | Contents | Estimate (full-time) |
|---|---|---|
| R13 | Evaluation harness (format, metrics, CLI, runs, compare, CI gate), the `acme` set, the synthetic corpus generator, a baseline of the current vector search | ~2 weeks |
| R14 | Shared filter function, full-text leg, fusion, in-process reranker with `erag reranker install` and degraded mode, threshold calibration, real tokenizer and table chunking, `erag reindex`, Admin > Retrieval | ~2.5 weeks |
| R15 | Admin > Evaluation, feedback and review queue, local faithfulness judge | ~1 week |
| R16 | Permission cache by epoch, selective-user search plan, restricted-document join, paginated users and audit log, bulk actions, CSV import and export, sessions, conversation retention, usage reports | ~1.5 weeks |
| R17 | `LimitStore` in Postgres, budget reservations, endpoint pools, fair generation queue across replicas, query embeddings with priority, advisory-lock singletons | ~2.5 weeks |
| R18 | Ingestion at scale (priorities, parallel embeddings, backoff, dead letters, reaper, back-pressure, streaming parse), S3 blob store, `halfvec` and HNSW tuning, PgBouncer compatibility, graceful shutdown | ~2.5 weeks |
| R19 | Metrics, alert rules, traces, correlation ids, health; pgBackRest, PITR, `erag backup verify` and the restore drill | ~1.5 weeks |
| R20 | Helm chart, CloudNativePG, Compose `scale` profile, expand/contract test, load tests (also with twice the replicas), benchmarks, operations guide, final README pass; Qdrant adapter only if needed (+1 week) | ~2 weeks |
| **Total** | | **about 15.5 weeks (16.5 with Qdrant), roughly 7 months part-time** |

R13 to R15 (quality) and R16 to R17 (users) are independent and can be reordered. R18 to R20 depend on R17 for the shared state. Every release gets a section in `PROGRESS.md` with what was built and verified, and updates the README for what it changed.

## 12. Decisions

**Taken (2026-11-18):**
1. **Reference size** accepted (section 2), as a benchmark target and not a ceiling. The software must keep scaling by adding replicas.
2. **The reranker runs inside the application process** (ONNX Runtime), not as a separate service.
3. **Shared limits in Postgres,** behind a `LimitStore` interface:
   - budget reservations must be transactional with the usage records;
   - the project already avoids Redis;
   - an UNLOGGED table handles thousands of updates per second.
   A Valkey backend stays in the ROADMAP, for an installation that ever outgrows it.
4. **SSO and SCIM are out of v0.5,** and so are IdP groups.
5. **Infrastructure stays in scope:** Helm, CloudNativePG, the Compose `scale` profile, pgBackRest and the restore drill in CI.

**Still open (defaults proposed, to confirm when the work reaches them):**
6. **Recovery targets:** RPO 15 minutes and RTO 4 hours.
7. **Embeddings:** stay on Ollama, or move to an in-process or dedicated embedding runtime if the benchmark shows ingestion is too slow. The model stays `bge-m3` either way.
8. **Qdrant in v0.5:** only if pgvector misses the target in the benchmark.
9. **Default reranker model:** chosen by the evaluation (R14).
