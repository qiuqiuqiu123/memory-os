# BGE-M3 CPU embedding service

This service loads real `BAAI/bge-m3` weights with SentenceTransformers on CPU,
returns normalized 1024-dimensional dense embeddings, and does not load sparse
or ColBERT heads. It never substitutes random vectors or lexical matching.

Merge `compose.bge.yml` with the main Compose file:

```sh
docker compose -f docker-compose.yml -f compose.bge.yml config --quiet
docker compose -f docker-compose.yml -f compose.bge.yml up -d --build
```

Set `MEMORY_OS_BGE_KEY` to a separate secret in `.env`. The override configures
API and worker to call `http://bge-m3:8080/v1`, use model `BAAI/bge-m3`, and expect
1024 dimensions. The service has no published host port. Model downloads persist
in `bge-model-cache`; restarting does not require fetching all weights again.
Changing from a previous embedding model requires a **new Qdrant collection and
reindexing existing memories**; identical dimensions alone do not imply compatible
vectors.

The CPU-only torch wheel is installed from `download.pytorch.org/whl/cpu` so the
image does not include CUDA libraries. The service runs one Uvicorn process and
serializes inference. Defaults are batch size 2, 512 tokens per text, and two CPU
threads. Each request accepts 1–64 strings. Long strings are truncated to the
configured token limit; usage reports retained input tokens. API callers can
request float arrays or base64-encoded little-endian float32 arrays. Only 1024
dimensions are accepted.

Default source is Hugging Face, with Compose setting
`HF_ENDPOINT=https://hf-mirror.com` for mainland-China connectivity. Set
`HF_ENDPOINT=https://huggingface.co` when the official endpoint is reachable.
The default pinned HF commit is
`5617a9f61b028005a4858fdac845db406aefb181`. The real weight file is
`pytorch_model.bin`, approximately 2.27 GB; this model revision does not expose
`model.safetensors`. Torch 2.6 loads tensor-only weights and remote Python code
is disabled.

An explicitly selected alternative is `BGE_SOURCE=modelscope`, pinned by default
to ModelScope commit `e44369c5623cc146f016da906583db4ee0e3488d`. Only model,
tokenizer, and standard SentenceTransformers configuration files are downloaded.
Each file is checked against the repository's SHA256 and size before use; the
weight checksum is
`b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38`.
The sources have different commit IDs; source switching is explicit and visible
in `/health`. A verified administrator-preloaded directory can instead be passed
with `BGE_MODEL_PATH` when running the image directly or adding a volume override.

First startup includes model download and loading. `/health` succeeds only after
the real model is loaded. Do not mistake a successful model URL probe for a
completed download or actual inference test. Compose permits a long initial
health-check startup period, bounds the container at 4GB RAM and two CPUs, and
uses low-memory model loading. Actual memory peak and inference latency must be
checked on the deployment host before increasing batch size, token length, or
parallelism. The host also needs disk space for the image, model cache, and
temporary download file; keep at least 8GB free for this component.
