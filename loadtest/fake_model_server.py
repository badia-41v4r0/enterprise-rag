"""A stand-in model server for load tests: Ollama-compatible embeddings and an OpenAI-compatible streaming chat
with a fixed latency. The load test then measures EnterpriseRAG (replicas, Postgres, the queue, reservations),
not the speed of a GPU we do not have.

    uv run python loadtest/fake_model_server.py --port 18080 --token-ms 40 --tokens 25

Embeddings are deterministic unit vectors derived from the text hash (dimension 1024, like bge-m3), so the
same text always maps to the same vector. It counts concurrent generations and reports the peak on /stats,
which shows whether the endpoint's max concurrency was respected.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import time
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

DIM = 1024
app = FastAPI()
state = {"in_flight": 0, "peak": 0, "generations": 0, "embeddings": 0}
cfg = {"token_ms": 40.0, "tokens": 25, "embed_ms": 5.0}


def vector(text: str) -> list[float]:
    rnd = random.Random(hashlib.sha256(text.encode()).digest())
    v = [rnd.gauss(0, 1) for _ in range(DIM)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


@app.get("/api/version")
async def version() -> dict[str, str]:
    return {"version": "fake"}


@app.get("/api/tags")
async def tags() -> dict[str, object]:
    return {"models": [{"name": "fake-chat"}, {"name": "bge-m3"}]}


@app.get("/stats")
async def stats() -> dict[str, int]:
    return state


@app.post("/v1/embeddings")
async def embeddings(req: Request) -> JSONResponse:
    body = await req.json()
    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
    await asyncio.sleep(cfg["embed_ms"] / 1000)
    state["embeddings"] += len(texts)
    return JSONResponse({"data": [{"index": i, "embedding": vector(t)} for i, t in enumerate(texts)]})


@app.post("/v1/chat/completions")
async def chat(req: Request) -> StreamingResponse:
    body = await req.json()
    prompt_chars = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))

    async def stream() -> AsyncIterator[bytes]:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            for i in range(int(cfg["tokens"])):
                await asyncio.sleep(cfg["token_ms"] / 1000)
                chunk = {"choices": [{"index": 0, "delta": {"content": f"word{i} "}}]}
                yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
            usage = {"prompt_tokens": prompt_chars // 4, "completion_tokens": int(cfg["tokens"])}
            done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}
            yield b"data: " + json.dumps(done).encode() + b"\n\ndata: [DONE]\n\n"
        finally:
            state["in_flight"] -= 1
            state["generations"] += 1

    return StreamingResponse(stream(), media_type="text/event-stream")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--token-ms", type=float, default=40.0)
    ap.add_argument("--tokens", type=int, default=25)
    ap.add_argument("--embed-ms", type=float, default=5.0)
    a = ap.parse_args()
    cfg.update(token_ms=a.token_ms, tokens=a.tokens, embed_ms=a.embed_ms)
    started = time.time()
    print(f"fake model server on 127.0.0.1:{a.port} (generation about {a.token_ms * a.tokens / 1000:.1f} s)")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    print(f"ran {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
