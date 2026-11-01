"""CPU BGE-M3 dense embeddings, using real local model weights only."""
import base64
import os
import secrets
import threading
from contextlib import asynccontextmanager
from typing import Literal

import numpy as np
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

MODEL_NAME = "BAAI/bge-m3"
REVISION = os.getenv("BGE_REVISION", "5617a9f61b028005a4858fdac845db406aefb181")
MAX_LENGTH = int(os.getenv("BGE_MAX_LENGTH", "512"))
BATCH_SIZE = int(os.getenv("BGE_BATCH_SIZE", "2"))
THREADS = int(os.getenv("BGE_THREADS", "2"))
API_KEY = os.getenv("MEMORY_OS_BGE_KEY", "")
model = None
inference_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    global model
    import torch
    from sentence_transformers import SentenceTransformer

    if not 1 <= MAX_LENGTH <= 8192 or not 1 <= BATCH_SIZE <= 8 or not 1 <= THREADS <= 8:
        raise RuntimeError("BGE length, batch size or thread configuration out of range")
    torch.set_num_threads(THREADS)
    torch.set_num_interop_threads(1)
    # A local directory supports administrator-preloaded, verified weights.
    # This never downloads or executes model-supplied Python code.
    from download_model import prepare_model
    source = prepare_model()
    model = SentenceTransformer(
        source,
        device="cpu",
        trust_remote_code=False,
        cache_folder=os.getenv("HF_HOME", "/models/huggingface"),
        # Official BGE-M3 ships pytorch_model.bin; torch>=2.6 weights_only
        # limits deserialization to tensors instead of arbitrary pickle objects.
        model_kwargs={"use_safetensors": False, "weights_only": True,
                      "low_cpu_mem_usage": True, "torch_dtype": torch.float32},
    )
    model.max_seq_length = MAX_LENGTH
    if model.get_sentence_embedding_dimension() != 1024:
        raise RuntimeError("BGE-M3 must return 1024 dimensions")
    yield
    model = None


app = FastAPI(title="Memory OS · BGE-M3 CPU", lifespan=lifespan)


class EmbeddingRequest(BaseModel):
    model: str = "BAAI/bge-m3"
    input: str | list[str]
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: int | None = Field(default=None)
    user: str | None = None


@app.get("/health")
def health():
    if model is None:
        raise HTTPException(503, "Model is not loaded")
    source = os.getenv("BGE_SOURCE", "huggingface")
    revision = os.getenv("BGE_MODELSCOPE_REVISION", "e44369c5623cc146f016da906583db4ee0e3488d") if source == "modelscope" else REVISION
    return {"status": "ok", "model": MODEL_NAME, "revision": revision,
            "source": source, "dimensions": 1024,
            "device": "cpu", "max_length": MAX_LENGTH, "batch_size": BATCH_SIZE}


@app.post("/v1/embeddings")
def embeddings(body: EmbeddingRequest, authorization: str | None = Header(default=None)):
    if API_KEY:
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not secrets.compare_digest(supplied, API_KEY):
            raise HTTPException(401, "Invalid embedding API key")
    if body.model not in (MODEL_NAME, "bge-m3"):
        raise HTTPException(400, "Only BAAI/bge-m3 is available")
    if body.dimensions not in (None, 1024):
        raise HTTPException(400, "BGE-M3 requires 1024 dimensions")
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not 1 <= len(texts) <= 64:
        raise HTTPException(400, "Input must contain 1..64 texts")
    if any(not text.strip() or len(text) > 32768 for text in texts):
        raise HTTPException(400, "Each text must contain 1..32768 characters")
    if model is None:
        raise HTTPException(503, "Model is not loaded")
    # Serialize model access even if HTTP clients concurrently send requests.
    # Internal batching bounds peak activation memory on a shared CPU server.
    with inference_lock:
        tokens = model.tokenizer(texts, truncation=True, max_length=MAX_LENGTH, padding=False)
        count = sum(len(ids) for ids in tokens["input_ids"])
        vectors = model.encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=True,
                               convert_to_numpy=True, show_progress_bar=False)
    data = []
    for i, vector in enumerate(vectors):
        encoded = vector.tolist() if body.encoding_format == "float" else base64.b64encode(
            np.asarray(vector, dtype="<f4").tobytes()).decode("ascii")
        data.append({"object": "embedding", "index": i, "embedding": encoded})
    return {"object": "list", "data": data, "model": MODEL_NAME,
            "usage": {"prompt_tokens": count, "total_tokens": count}}
