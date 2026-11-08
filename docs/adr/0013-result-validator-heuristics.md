# 0013. Result validator: size cap, classification floor, injection heuristics

**Status:** Accepted (2026-11-08) — **Owner review**

## Decision
- Results larger than `ERAG_TOOL_RESULT_MAX_BYTES` (16 kB) are refused, not truncated (a truncated JSON is misleading).
- Result classification = max(tool's declared floor, every `classification` field the server reports). It can only go up.
- Any string value matching an instruction-like pattern ("ignore previous instructions", "you are now", "call the tool X", "reveal ... salary", our own delimiter tags, ...) is **replaced entirely** by `[removed: possible prompt injection]` and the call is flagged in the audit log. Partial redaction was tried first and left a working instruction ("... and call list_invoices ...") behind, which the test caught.
- Retrieved document chunks are *not* scrubbed (they are delimited and marked untrusted instead), so that legitimate documents that quote such phrases stay searchable.

## Consequences
Heuristics are not a guarantee: they reduce, not remove, injection risk. False positives drop legitimate strings (for example a file whose text says "reveal the budget"). The real guarantees are structural: the model only sees tools and data the user is authorized for, and tool calls are re-authorized server-side.
