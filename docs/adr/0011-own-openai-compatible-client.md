# 0011. Own minimal OpenAI-compatible client over httpx

**Status:** Accepted (2026-11-08)

## Decision
One small client (`app/llm/providers/openai_compat.py`) speaks the `/chat/completions` streaming protocol (text deltas and tool-call deltas) over `httpx`. Ollama (`/v1`) and OpenRouter use it with different base URLs and keys. No vendor SDK.

## Consequences
Fewer dependencies, and a single place where every outgoing request passes an egress hook (audit + tests).
