# 0003. Classification routing after retrieval (and after every tool result)

**Status:** Accepted (2026-11-08)

## Context
Containers are classified `public < internal < confidential`. Which model may process a request depends on the data it will see, which is only known after retrieval.

## Decision
- The router runs **after** retrieval. Effective classification = max over: retrieved chunks, conversation history included in the prompt, and tool results.
- Policy (configurable): local models may process everything; cloud models (OpenRouter) at most `ERAG_CLOUD_MAX_CLASSIFICATION` (default `internal`). `confidential` is never sent to a cloud model, whatever the setting.
- If the user asks for a specific model, the router honours it only when the policy allows it; otherwise it picks a permitted model, and if none is available it refuses with an explanation.
- Tool loop: after each tool result the classification is recomputed. If the current model is no longer permitted, the loop switches to a permitted (local) model before the result is sent anywhere; if none exists, the request stops with a refusal. The cloud model never receives the higher-classified result.
- Every outgoing LLM request passes through an egress hook that records destination, model and classification in the audit log.

## Consequences
Answers grounded in confidential data are always generated locally (slower on the reference hardware). Tested by asserting on the outgoing requests, not on model prose.
