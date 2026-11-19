"""The ingestion embedder spreads batches over an endpoint pool with bounded concurrency (R17, R18)."""

from __future__ import annotations

import asyncio
import json

import httpx

from app.ingestion.embedder import OllamaEmbedder


async def test_ingest_embedder_spreads_batches_over_the_pool_and_keeps_order() -> None:
    hosts: list[str] = []
    in_flight = 0
    peak = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        hosts.append(req.url.host)
        batch = json.loads(req.content)["input"]
        await asyncio.sleep(0.02 if "t-0" in batch else 0.005)  # the first batch finishes last
        in_flight -= 1
        data = [{"index": i, "embedding": [float(t.split("-")[1]), 0.0]} for i, t in enumerate(batch)]
        return httpx.Response(200, json={"data": data})

    emb = OllamaEmbedder(
        base_urls=["http://gpu-a:11434", "http://gpu-b:11434"],
        model="m",
        dim=2,
        concurrency=3,
        batch_size=2,
        transport=httpx.MockTransport(handler),
    )
    vectors = await emb.embed([f"t-{i}" for i in range(11)])
    assert [v[0] for v in vectors] == [float(i) for i in range(11)]  # input order, whatever finished first
    assert set(hosts) == {"gpu-a", "gpu-b"} and len(hosts) == 6
    assert 1 < peak <= 3
