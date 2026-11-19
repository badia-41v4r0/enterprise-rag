# Roadmap

Items outside the scope of the MVP (docs/PLAN.md section 1), v0.2 (docs/PLAN-v0.2.md), v0.3 (docs/PLAN-v0.3.md) and v0.5 (docs/PLAN-v0.5.md), or cut for time.

## Cut from v0.2 (planned, not implemented)

- **Allowlisted egress for runner MCPs**: a per-MCP forward proxy that only permits listed domains. Runner MCPs have network `none` only (ADR 0022).
- **Runner on Podman / rootless engines**: the engine interface is neutral, but only Docker was tested.
- **Web connector polish**: robots.txt, per-site request pacing, nested sitemap indexes, JavaScript rendering.

## Left open in v0.3

- `max_completion_tokens` for OpenAI reasoning models (only `max_tokens` is sent).
- First real calls to the official Jev API and the OpenRouter Jev Router (built from the documentation, ADR 0032); record the model the Jev Router actually served.
- An open decider on a GPU (Open-Jev 2B takes about a minute per decision on this machine's CPU, ADR 0031) and a quality benchmark of deciders on our own questions.
- Periodic re-evaluation of provider and decider locality (today at router build and per request).

## Left open in v0.5

- **SSO, SCIM and IdP groups** (out of v0.5 by decision 4).
- **A Valkey `LimitStore`** for installations that outgrow rate limits in Postgres (ADR 0043).
- **Qdrant `VectorStore`**: only if pgvector misses the target at 20 million chunks (decision 8, ADR 0050). The synthetic benchmark at that size needs a larger host than the development machine.
- **Streaming parsers** for very large PDF and DOCX files (today: a memory cap that refuses them before reading).
- **The previous release's test suite against the new schema** in CI (today: a static expand/contract check on the migration files, ADR 0053), and the restore drill in CI.
- **A real Kubernetes install** of the Helm chart and CloudNativePG (validated with `helm lint` and `kubeconform`, not installed).
- **Load tests against real GPUs** and the 50,000 documents per day ingestion target (the R20 load test uses a stand-in model server).
- Finer permission-cache invalidation (today one global epoch: any permission change invalidates every cached entry).

## Later

- Mirroring source-system ACLs for connectors; OAuth connectors (Google Drive, SharePoint, S3, Git, Confluence).
- Multi-tenancy; delegated OAuth to M365/Google.
- Deny rules (the model is allow-only).
- Long-lived MCP sessions and connection pooling (one session per call today).
- Tamper-evident audit log (hash chain or external sink).
- Hardware-backed master key (KMS/HSM/TPM) for the credential store.
- English version of the original architecture diagram as an editable Excalidraw file (a Mermaid version is in `docs/architecture.md`).
- **Credential hardening** (see white paper 7.3): a pluggable master-key provider backed by a key manager (cloud KMS, HSM or a vault service) or a manual unlock at boot, and a separate credential-broker service so the web application never holds secrets.

## Done in v0.5 (removed from the lists above)

Budget reservations and the tokens of a request that fails mid-stream (ADR 0044); a reaper for sync runs left running (R18); hybrid search and a reranker (ADR 0046, 0047); an S3 `BlobStore` (ADR 0050); Helm and horizontal scaling with shared rate limits (ADR 0049, 0053).
