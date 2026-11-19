"""Pinned model files and their explicit installation (`erag models install <name>`).

Nothing is ever downloaded automatically. Each model names a repository, a pinned revision and the SHA-256 of
every file; `install` fetches exactly those bytes (or copies them from a local directory for offline
installations) and refuses anything whose hash differs. Licenses are permissive only (decision D8).
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.config import get_settings


@dataclass(frozen=True)
class ModelFile:
    remote: str  # path inside the repository
    local: str  # file name in the model directory
    sha256: str
    size_mb: int


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str  # "reranker" | "tokenizer"
    repo: str
    revision: str
    license: str
    files: tuple[ModelFile, ...]
    max_length: int = 512
    description: str = ""

    @property
    def size_mb(self) -> int:
        return sum(f.size_mb for f in self.files)


MODELS: dict[str, ModelSpec] = {
    m.name: m
    for m in (
        ModelSpec(
            name="mmarco-minilm-l12",
            kind="reranker",
            repo="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
            revision="1427fd652930e4ba29e8149678df786c240d8825",
            license="Apache-2.0",
            files=(
                ModelFile(
                    "onnx/model_quint8_avx2.onnx",
                    "model.onnx",
                    "6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b",
                    113,
                ),
                ModelFile(
                    "tokenizer.json",
                    "tokenizer.json",
                    "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626",
                    16,
                ),
            ),
            description="Small multilingual cross-encoder (118M parameters, int8, AVX2)",
        ),
        ModelSpec(
            name="bge-reranker-v2-m3",
            kind="reranker",
            repo="onnx-community/bge-reranker-v2-m3-ONNX",
            revision="6f5ff65298512715a1e669753bc754d2bc8f367b",
            license="Apache-2.0 (BAAI/bge-reranker-v2-m3, ONNX export by onnx-community)",
            files=(
                ModelFile(
                    "onnx/model_int8.onnx",
                    "model.onnx",
                    "912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d",
                    544,
                ),
                ModelFile(
                    "tokenizer.json",
                    "tokenizer.json",
                    "8bf8afbfd11306bd872018c53bfdf2e160a56f8edbcf49933324404791c148d3",
                    16,
                ),
            ),
            description="Multilingual cross-encoder of the bge-m3 family (568M parameters, int8)",
        ),
        ModelSpec(
            name="bge-m3-tokenizer",
            kind="tokenizer",
            repo="BAAI/bge-m3",
            revision="5617a9f61b028005a4858fdac845db406aefb181",
            license="MIT",
            files=(
                ModelFile(
                    "tokenizer.json",
                    "tokenizer.json",
                    "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08",
                    16,
                ),
            ),
            max_length=8192,
            description="Tokenizer of the bge-m3 embedding model, for exact token counts",
        ),
    )
}


class ModelError(RuntimeError):
    pass


def models_dir() -> Path:
    return get_settings().models_dir


def model_path(name: str) -> Path:
    return models_dir() / name


def installed(name: str) -> bool:
    spec = MODELS.get(name)
    return spec is not None and all((model_path(name) / f.local).is_file() for f in spec.files)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def install(name: str, *, source_dir: Path | None = None, progress: bool = True) -> Path:
    """Download (or copy from `source_dir`) every file of `name`, verify its hash, and move it into place."""
    spec = MODELS.get(name)
    if spec is None:
        raise ModelError(f"unknown model {name}; known: {', '.join(sorted(MODELS))}")
    target = model_path(name)
    target.mkdir(parents=True, exist_ok=True)
    for f in spec.files:
        final = target / f.local
        if final.is_file() and _sha256(final) == f.sha256:
            continue
        tmp = target / (f.local + ".part")
        if source_dir is not None:
            src = source_dir / f.local
            if not src.is_file():
                src = source_dir / f.remote
            shutil.copyfile(src, tmp)
        else:
            url = f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{f.remote}"
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done = 0
                with tmp.open("wb") as out:
                    for chunk in r.iter_bytes(1 << 20):
                        out.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            print(f"\r  {f.local}: {done * 100 // total:3d}% of {total >> 20} MB", end="", flush=True)
                if progress:
                    print()
        digest = _sha256(tmp)
        if digest != f.sha256:
            tmp.unlink(missing_ok=True)
            raise ModelError(f"{name}/{f.local}: SHA-256 {digest} does not match the pinned {f.sha256}")
        tmp.replace(final)
    return target
