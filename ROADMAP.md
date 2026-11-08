# Roadmap

Items outside the scope of the MVP (docs/PLAN.md section 1) and of v0.2 (docs/PLAN-v0.2.md), or cut for time.

## Cut from v0.2 (planned, not implemented)

- **Allowlisted egress for runner MCPs**: a per-MCP forward proxy that only permits listed domains. Runner MCPs have network `none` only (ADR 0022).
- **Runner on Podman / rootless engines**: the engine interface is neutral, but only Docker was tested.
- **Web connector polish**: robots.txt, per-site request pacing, nested sitemap indexes, JavaScript rendering.
- A reaper for sync runs left `running` by a crashed worker.

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
