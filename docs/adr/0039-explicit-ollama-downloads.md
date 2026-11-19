# 0039. Ollama models are downloaded only on an explicit, confirmed administrator action

**Status:** Accepted (2026-11-09), owner decision 3 of PLAN-v0.4

## Decision
- Nothing is ever downloaded automatically: no endpoint, check or start-up hook calls Ollama's `/api/pull` except
  `POST .../models/download`, and the Test buttons use `/api/chat` and `/v1/embeddings`, which never pull.
- **Two calls.** `POST .../models/download/preview {model}` validates an Ollama library name (`name[:tag]` or
  `namespace/name[:tag]`; other registries are not offered) and reads the model's **manifest** from the Ollama
  registry (`ERAG_OLLAMA_REGISTRY_URL`, default `https://registry.ollama.ai`, public addresses only, through the
  destination guard, no redirects, 200 kB cap) to show the exact size; the model itself is not fetched. The UI shows
  a confirmation with the name, the size and "from the Ollama registry". `POST .../models/download {model,
  size_bytes, confirm: true}` re-reads the manifest and refuses if the size changed, then starts the pull in the
  local Ollama in the background.
- **Progress and cancel.** `GET .../models/download/{id}` reports status, bytes done and total (from Ollama's
  streamed progress); `DELETE` cancels by closing the stream, which stops Ollama's pull. Start, completion, failure
  and cancellation are audited (`setup.model.download.*`) with the model name and size.
- Suggested models are listed only if their licenses are permissive: `bge-m3` (MIT) and
  `qwen3:4b-instruct-2507-q4_K_M` (Apache-2.0). Administrators may type any other library name; its license is
  their choice and the README says so.

## Consequences
- Downloads live in the app process (one per model at a time); a restart forgets the progress view, and Ollama may
  keep partial files that its next pull of the same model resumes.
- Tests: `tests/setup/test_models_download.py` checks that listing, environment checks, previews and Tests leave the
  real Ollama's model list unchanged, and (opt-in, `ERAG_TEST_OLLAMA_DOWNLOAD=1`) downloads the small `all-minilm`
  model for real through the endpoint, cancels a second download, and removes the model afterwards with `ollama rm`.
