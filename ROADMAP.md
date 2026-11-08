# Roadmap (beyond the MVP)

Items intentionally out of the MVP scope (docs/PLAN.md section 1) or cut for time.

- Multi-tenancy; SSO/OIDC; delegated OAuth to M365/Google; external connectors (SharePoint, Drive).
- Deny rules (the MVP is allow-only).
- Hybrid search (pgvector + Postgres FTS) and a reranker (cut list items 1 and 2; not implemented).
- OpenRouter monthly spend ceiling enforced by the app (configured as `ERAG_OPENROUTER_MONTHLY_BUDGET_USD`, **not enforced yet**).
- Long-lived MCP sessions or Streamable HTTP transport instead of one stdio process per call.
- Qdrant `VectorStore` and S3/MinIO `BlobStore` adapters.
- Kubernetes/Helm, horizontal scaling (the rate limiters are in-process).
- Streaming of tool-loop intermediate text in the UI is basic; no per-message feedback, no export.
- English version of the original architecture diagram as an editable Excalidraw file (a Mermaid version is in `docs/architecture.md`).
