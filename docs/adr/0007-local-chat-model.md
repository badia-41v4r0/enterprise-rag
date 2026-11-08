# 0007. Local chat model: `qwen3:4b-instruct-2507-q4_K_M`

**Status:** Accepted (2026-11-08); amended 2026-11-08 after owner review (switched from the thinking variant)

## Context
GTX 1660 SUPER (6 GB VRAM). We may pull one small model (<= ~3 GB) that supports tool calling. Candidates: Qwen3 1.7B/4B, Llama 3.2 3B, Gemma 3 4B.

## Decision
`qwen3:4b` (Q4_K_M, 2.5 GB). Checked with a real call through Ollama's OpenAI-compatible endpoint: it emitted a correct `list_invoices(min_amount=10000, status="open")` tool call. It fits on the GPU next to `bge-m3` (about 3.7 GB VRAM loaded with a 4k context). Gemma 3 has no native tool calling in Ollama; Qwen3 is Apache-2.0.

## Findings
- The current `qwen3:4b` tag is the 2507 *thinking* variant: it always reasons first (`think: false` and `/no_think` are ignored; `reasoning_effort` is rejected). The reasoning arrives inline in `content`, terminated by `</think>`. The provider strips everything up to `</think>` before anything reaches the user or is stored.
- Throughput is about 50 tokens/s, so a grounded answer takes roughly 10 to 40 s, most of it reasoning.

## Amendment: non-thinking variant (owner approved)
The default is now `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB, same size and licence, native tool calling, no reasoning preamble). `ERAG_LOCAL_CHAT_STRIP_THINK` defaults to `false` so answers stream immediately; set it to `true` only if you switch to a thinking model. The old `qwen3:4b` can be removed from Ollama if the disk space matters.
