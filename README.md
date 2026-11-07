# EnterpriseRAG

**Permission-aware RAG + MCP for enterprise generative AI.** A self-hosted reference implementation by [Badia Partners](https://github.com/badia-41v4r0).

> **Status: early development.** Nothing is runnable yet. See [`docs/PLAN.md`](docs/PLAN.md) for the roadmap.

## The idea

Most enterprise RAG and agent setups let the model see (or reach) more than the person asking is allowed to see. EnterpriseRAG is built around a few principles:

1. The model never decides permissions; the backend does.
2. Access control is applied during retrieval, before the LLM sees anything.
3. Every tool call goes through a gateway with authorization and audit, and tools only receive data the user is already allowed to see.
4. Data classification decides which models (local or cloud) may process it.
5. Secrets never reach the browser or the model.

## Planned scope (MVP)

Single tenant, one admin who manages roles and permissions over containers and documents. Python/FastAPI backend, PostgreSQL + pgvector, React frontend, local models via Ollama and cloud models via OpenRouter, MCP gateway with reference servers, and a fictional demo company (ACME Corp).

## License

Apache-2.0. See [`LICENSE`](LICENSE).

## Consulting and implementation

Need help adopting this in your company? Implementation and consulting are offered by Badia Partners: https://badiapartners.com
