"""Dev-only provider that shells out to the headless Claude Code CLI (`claude -p`).

Disabled by default (`ERAG_CLAUDE_CODE_ENABLED=false`). It is never part of the demo or the
production path (PLAN.md section 13). It is treated as a *cloud* model: never cleared for
confidential data. Built-in tools are disabled; tool calls come back as structured output.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from typing import Any

from app.llm.providers.base import (
    Done,
    EgressRecord,
    Locality,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallsEvent,
    ToolSpec,
    check_clearance,
    emit_egress,
)
from app.models import Classification

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
                "required": ["name", "arguments"],
            },
        },
    },
    "required": ["answer"],
}


def _render(messages: list[dict[str, Any]], tools: list[ToolSpec] | None) -> tuple[str, str]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    lines = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            lines.append(f"[tool result {m.get('tool_call_id', '')}]\n{m['content']}")
        elif m.get("tool_calls"):
            lines.append(f"[assistant called tools] {json.dumps(m['tool_calls'])}")
        else:
            lines.append(f"[{m['role']}]\n{m['content']}")
    if tools:
        system += (
            "\n\nYou may call these tools by returning them in `tool_calls` (leave `answer` empty when calling):\n"
            + json.dumps([t.to_openai()["function"] for t in tools])
        )
    return system, "\n\n".join(lines)


class ClaudeCodeProvider:
    provider = "claude-code"
    locality: Locality = "cloud"
    max_classification = Classification.internal

    def __init__(self, binary: str = "claude", model: str = "default", timeout: float = 180.0) -> None:
        self.binary = binary
        self.model = model
        self.timeout = timeout

    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self.model}"

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None = None,
        classification: Classification,
    ) -> AsyncIterator[StreamEvent]:
        check_clearance(self.max_classification, classification, self.model_id)
        exe = shutil.which(self.binary)
        if exe is None:
            raise ProviderError("claude CLI not found")
        system, prompt = _render(messages, tools)
        args = [
            exe,
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--no-session-persistence",
            "--system-prompt",
            system,
            "--json-schema",
            json.dumps(OUTPUT_SCHEMA),
        ]
        if self.model != "default":
            args += ["--model", self.model]
        await emit_egress(
            EgressRecord(
                self.provider,
                self.model,
                self.locality,
                "claude-cli",
                classification,
                {"system": system, "prompt": prompt},
            )
        )
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout)
        except TimeoutError as exc:
            proc.kill()
            raise ProviderError("claude CLI timed out") from exc
        if proc.returncode != 0:
            raise ProviderError(f"claude CLI failed: {err.decode(errors='replace')[:300]}")
        envelope = json.loads(out.decode())
        structured = envelope.get("structured_output")
        if structured is None:
            try:
                structured = json.loads(envelope.get("result") or "{}")
            except json.JSONDecodeError:
                structured = {"answer": envelope.get("result", "")}
        calls = structured.get("tool_calls") or []
        if calls:
            yield ToolCallsEvent(
                [
                    ToolCall(id=f"call_{i}", name=c["name"], arguments=json.dumps(c["arguments"]))
                    for i, c in enumerate(calls)
                ]
            )
            yield Done("tool_calls")
            return
        yield TextDelta(structured.get("answer", ""))
        yield Done("stop", envelope.get("usage") or {})
