# EnterpriseRAG

**Permission-aware RAG + MCP for enterprise generative AI.** A self-hosted reference implementation by [Badia Partners](https://badiapartners.com).

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

EnterpriseRAG is **source-available**, not open source.

- **Software:** [PolyForm Noncommercial License 1.0.0](LICENSE). You can read, run, modify and share it for noncommercial purposes (personal use, research, study, hobby projects, and use by educational, charitable, public-research and government institutions).
- **Commercial use** (including running it inside a company for business purposes) requires a commercial license from the licensor, Alvaro Badia (Badia Partners). See [`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md).
- **White paper:** [CC BY 4.0](docs/whitepaper/LICENSE-CC-BY-4.0.txt), see [`docs/whitepaper/`](docs/whitepaper/).

### 60-day evaluation permit

In addition to the license above, the licensor (Alvaro Badia, under the Badia Partners brand) grants any company or organization permission to install and run EnterpriseRAG for **evaluation only**, for up to **60 days** from first installation, under these conditions:

- Evaluation means testing whether the software fits your needs, in a non-production environment. No production use and no use to operate your business.
- Do not redistribute it or offer it to third parties.
- After 60 days, stop using it or obtain a commercial license.
- You do not need to ask or notify us. This permit is provided as-is and may be withdrawn for future versions.

## Consulting and implementation

Need help adopting this in your company? Implementation and consulting are offered by Badia Partners: https://badiapartners.com
