"""Cross-encoder reranker running inside the application process (ADR 0047).

ONNX Runtime and `tokenizers` score (question, chunk) pairs. Inference runs in a thread pool (ONNX Runtime
releases the GIL), at most `reranker_concurrency` batches at a time per process, and a request that cannot get a
slot and finish within `reranker_timeout_seconds` falls back to the fused order. The reranker never sends text
anywhere: it reads only chunks that already passed the authorization filter and the ACL recheck.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import Sequence
from functools import partial
from typing import Any

import numpy as np

from app.core.config import get_settings
from app.ml.models import MODELS, installed, model_path

log = logging.getLogger(__name__)


class RerankerUnavailable(RuntimeError):
    """No model installed, the model failed to load, or no slot/answer in time. The caller falls back."""


class CrossEncoder:
    def __init__(self, name: str) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        spec = MODELS[name]
        if spec.kind != "reranker":
            raise ValueError(f"{name} is not a reranker")
        if not installed(name):
            raise RerankerUnavailable(f"reranker {name} is not installed (erag models install {name})")
        s = get_settings()
        base = model_path(name)
        opts = ort.SessionOptions()
        if s.reranker_threads:
            opts.intra_op_num_threads = s.reranker_threads
        providers = ["CPUExecutionProvider"]
        if s.reranker_use_cuda and "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")
        self.name = name
        self.max_length = spec.max_length
        self.session = ort.InferenceSession(str(base / "model.onnx"), sess_options=opts, providers=providers)
        self.inputs = {i.name for i in self.session.get_inputs()}
        tok = Tokenizer.from_file(str(base / "tokenizer.json"))
        tok.enable_truncation(max_length=self.max_length)
        tok.enable_padding()
        self.tokenizer = tok

    def score(self, query: str, passages: Sequence[str], batch_size: int = 16) -> list[float]:
        """Relevance in (0, 1): the sigmoid of the model's logit for each (query, passage) pair."""
        out: list[float] = []
        for start in range(0, len(passages), batch_size):
            batch = [(query, p) for p in passages[start : start + batch_size]]
            enc = self.tokenizer.encode_batch(batch)
            feed: dict[str, Any] = {
                "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
            }
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
            logits = self.session.run(None, feed)[0]
            for row in np.asarray(logits).reshape(len(batch), -1):
                out.append(1.0 / (1.0 + math.exp(-float(row[0]))))
        return out


_lock = threading.Lock()
_models: dict[str, CrossEncoder] = {}
_failed: dict[str, str] = {}
_semaphores: dict[int, asyncio.Semaphore] = {}


def load(name: str) -> CrossEncoder:
    """Load once per process (thread-safe). Raises RerankerUnavailable when it cannot be used."""
    with _lock:
        if name in _models:
            return _models[name]
        if name in _failed:
            raise RerankerUnavailable(_failed[name])
        try:
            model = CrossEncoder(name)
        except RerankerUnavailable:
            raise
        except Exception as exc:  # a corrupt or incompatible model file must not break retrieval
            _failed[name] = f"reranker {name} failed to load: {exc}"
            log.exception("reranker failed to load")
            raise RerankerUnavailable(_failed[name]) from exc
        _models[name] = model
        log.info("reranker loaded", extra={"model": name})
        return model


def status(name: str) -> dict[str, Any]:
    return {
        "model": name,
        "installed": installed(name),
        "loaded": name in _models,
        "error": _failed.get(name),
    }


def reset() -> None:
    with _lock:
        _models.clear()
        _failed.clear()


def _semaphore() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    sem = _semaphores.get(loop_id)
    if sem is None:
        sem = _semaphores[loop_id] = asyncio.Semaphore(max(1, get_settings().reranker_concurrency))
    return sem


async def rerank(name: str, query: str, passages: Sequence[str]) -> list[float]:
    """Scores for `passages`, or RerankerUnavailable (missing model, overload, timeout, failure)."""
    if not passages:
        return []
    timeout = get_settings().reranker_timeout_seconds
    loop = asyncio.get_running_loop()

    async def run() -> list[float]:
        async with _semaphore():
            model = await loop.run_in_executor(None, load, name)
            return await loop.run_in_executor(None, partial(model.score, query, list(passages)))

    try:
        return await asyncio.wait_for(run(), timeout)
    except RerankerUnavailable:
        raise
    except TimeoutError as exc:
        raise RerankerUnavailable(f"reranker {name} did not answer within {timeout:g} s") from exc
    except Exception as exc:
        log.exception("reranker failed")
        raise RerankerUnavailable(f"reranker {name} failed: {exc}") from exc
