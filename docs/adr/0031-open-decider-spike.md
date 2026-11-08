# 0031. Open decider spike: Open-Jev 2B runs on this machine's CPU (about 1 minute per decision)

**Status:** Accepted (2026-11-11), result of the PLAN-v0.3 R8 spike (section 6). **Owner review** (whether to use it).

## What was tried
- **Candidate**: `ZefanCai/Open-Jev-2B` (Hugging Face, revision `0c7aa498b1627be8da4acf34c863ff0ee0a92785`). Licenses checked before use: adapter and decision head **Apache-2.0** (model card and `LICENSE` in the package), Open-Jev code **MIT** (`LICENSE`), base model `Qwen/Qwen3.5-2B` revision `15852e8c...` **Apache-2.0** (its `LICENSE` and card). All within D8. No smaller Apache/MIT/BSD candidate was clearly easier (DecisionNode Edge Mini is AGPL and excluded; pjev-2b's license was not checked).
- **Isolation**: a separate virtual environment in `~/.cache/erag-open-jev` (torch 2.15.0 CPU wheel, transformers 5.10.2, peft 0.19.1, accelerate 1.13.0), outside the project; `pyproject.toml` and `uv.lock` gained nothing (the license check is unchanged).
- **Memory safety**: the server ran in a `systemd-run --user --scope` with `MemoryMax=6500M` and `MemorySwapMax=1500M` so an out-of-memory condition could only kill the server; the two Ollama models were unloaded first (restored afterwards). Available memory before start: 7.2 GB, then 8.3 GB after unloading.
- **Command** (CPU, bfloat16 as released, 6 threads): `HF_HUB_OFFLINE=1 OMP_NUM_THREADS=6 python -m jev.server --checkpoint models/Open-Jev-2B/package/checkpoint --device cpu --max-length 4096 --batch-size 1 --prefix-cache --host 127.0.0.1 --port 8792`. Port 8791 (the project's documented default) is taken on this machine by the demo `mcp-office` server, so 8792 was used.

## Results
- Start-up about 10 s (weights memory-mapped); resident memory about 3.3 GB.
- **Protocol confirmed** against the real server: `POST /v1/systemone` with `state` and `questions` of types `choice`, `score` and `noul` (spelled so), answers exactly as in TypeSafe's reference (`choice`/`confidence`/`probabilities`; `score`/`confidence`/`legend`/`probabilities` with string level keys; `noul` probability), plus `usage` (`input_tokens`, `output_tokens: 0`) and a `metadata` object (ignored). `GET /health` and `GET /v1/models` (aliases `open-jev`, `jev-latest`) exist. No `model` field is needed in the request.
- **Latency on CPU**: the project's example request (7 candidate sequences) 53 s; our router request (13 candidate sequences, about 1,800 input tokens) **155 s without and 58 s with `--prefix-cache`**. The GPU (6 GB, Turing, no bf16) was not used: the documented peak for the 2B is 6.9 GiB.
- **Quality, informally**: "Show me open invoices over 10k EUR" -> question_answering (0.68), difficulty 2.6, needs tools p=0.70; "hello!" -> chit_chat (0.85), needs tools p=0.006. Plausible, in line with the leaderboard's ~48 for this model; not benchmarked.
- **Through the application**: the opt-in test `decider_open` passed (raw protocol call plus a decider created through the admin API, local, cleared for confidential, **Test** OK), and a live dry run on the stack for the finance persona at `confidential` classification used the local Open-Jev decider (58.7 s) and kept the local model.

## Decision
- The open decider adapter (`kind = protocol`) is implemented and tested against the real server; tests are opt-in (`decider_open`, `ERAG_TEST_OPEN_JEV_URL`), because the server is not part of the stack.
- On this hardware a 1-minute decision is too slow for interactive chat: the **prompted LLM decider** on the local qwen model (about 3 s) remains the practical choice, with Open-Jev useful for evaluation, dry runs and a GPU machine. The README documents how to start it (with `--prefix-cache` and a raised decider timeout).
- Nothing from the spike remains running; the environment in `~/.cache/erag-open-jev` (about 1 GB plus 4.5 GB of base weights in the Hugging Face cache) can be deleted.
