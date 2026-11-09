# Roadmap

Items outside the scope of the MVP (docs/PLAN.md section 1), v0.2 (docs/PLAN-v0.2.md) and v0.3 (docs/PLAN-v0.3.md), or cut for time.

## Cut from v0.2 (planned, not implemented)

- **Allowlisted egress for runner MCPs**: a per-MCP forward proxy that only permits listed domains. Runner MCPs have network `none` only (ADR 0022).
- **Runner on Podman / rootless engines**: the engine interface is neutral, but only Docker was tested.
- **Web connector polish**: robots.txt, per-site request pacing, nested sitemap indexes, JavaScript rendering.
- A reaper for sync runs left `running` by a crashed worker.

## Left open in v0.3

- **Budget reservations** instead of checks: today concurrent requests can each pass the worst-case check and overshoot a budget or a pool by their own worst cases (ADR 0026).
- Record the tokens of a request that fails mid-stream (only the request is counted today).
- `max_completion_tokens` for OpenAI reasoning models (only `max_tokens` is sent).
- First real calls to the official Jev API and the OpenRouter Jev Router (built from the documentation, ADR 0032); record the model the Jev Router actually served.
- An open decider on a GPU (Open-Jev 2B takes about a minute per decision on this machine's CPU, ADR 0031) and a quality benchmark of deciders on our own questions.
- Periodic re-evaluation of provider and decider locality (today at router build and per request).

## Left open in v0.4

- Translations of the interface (the setup texts are already in one file, `frontend/src/lib/texts.ts`; older screens still keep theirs inline).
- Email invitations and SSO for the people step (no mail server is assumed; temporary passwords are handed out by the administrator).
- A legal review of the license notice shown in the wizard (ADR 0035).
- Model downloads that survive an application restart (the progress view lives in the app process today).
- Exporting the setup report as PDF; a signed (tamper-evident) setup record.

## Later

- Mirroring source-system ACLs for connectors; OAuth connectors (Google Drive, SharePoint, S3, Git, Confluence).
- Multi-tenancy; SSO/OIDC; delegated OAuth to M365/Google.
- Deny rules (the model is allow-only).
- Hybrid search (pgvector + Postgres FTS) and a reranker.
- Long-lived MCP sessions and connection pooling (one session per call today).
- Tamper-evident audit log (hash chain or external sink).
- Qdrant `VectorStore` and S3/MinIO `BlobStore` adapters.
- Kubernetes/Helm, horizontal scaling (rate limiters are in-process).
- Hardware-backed master key (KMS/HSM/TPM) for the credential store.
- English version of the original architecture diagram as an editable Excalidraw file (a Mermaid version is in `docs/architecture.md`).
- **Credential hardening** (see white paper 7.3): a pluggable master-key provider backed by a key manager (cloud KMS, HSM or a vault service) or a manual unlock at boot, and a separate credential-broker service so the web application never holds secrets.
