"""Embedding backends for the index: OpenAI, Gemini, or a local model.

Provider/model come from EMBED_PROVIDER / EMBED_MODEL in .env (or function
arguments). Defaults per provider:

    openai  text-embedding-3-small   (needs OPENAI_API_KEY)
    gemini  gemini-embedding-001     (needs GEMINI_API_KEY)
    local   BAAI/bge-small-en-v1.5   (no key; fastembed downloads the ONNX
                                      model on first use, ~130 MB)

Queries must be embedded with the same provider/model that built the index;
search.py reads both from index_meta.json.
"""

import os

import numpy as np

DEFAULT_MODELS = {
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-001",
    "local": "BAAI/bge-small-en-v1.5",
}

API_BATCH = 100  # Gemini's OpenAI-compat endpoint caps batches at 100


def embed_config(provider: str | None = None,
                 model: str | None = None) -> tuple[str, str]:
    provider = (provider or os.environ.get("EMBED_PROVIDER") or "openai").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unsupported embedding provider {provider!r}; "
                         f"known: {list(DEFAULT_MODELS)}")
    model = model or os.environ.get("EMBED_MODEL") or DEFAULT_MODELS[provider]
    return provider, model


def _local_model(model: str):
    from fastembed import TextEmbedding
    return TextEmbedding(model)


def _api_client(provider: str):
    from llm_client import LLMClient
    return LLMClient(provider).raw


def embed_texts(texts: list[str], provider: str, model: str,
                progress: bool = True) -> np.ndarray:
    """Embed documents/records."""
    if provider == "local":
        vectors = list(_local_model(model).embed(texts, batch_size=64))
        return np.asarray(vectors, dtype=np.float32)
    client = _api_client(provider)
    vectors = []
    for i in range(0, len(texts), API_BATCH):
        resp = client.embeddings.create(model=model,
                                        input=texts[i:i + API_BATCH])
        vectors.extend(d.embedding for d in resp.data)
        if progress:
            print(f"  embedded {min(i + API_BATCH, len(texts))}/{len(texts)}")
    return np.asarray(vectors, dtype=np.float32)


def embed_query(query: str, provider: str, model: str) -> np.ndarray:
    """Embed a search query (BGE-style local models use a query prefix)."""
    if provider == "local":
        return np.asarray(list(_local_model(model).query_embed(query)),
                          dtype=np.float32)
    client = _api_client(provider)
    resp = client.embeddings.create(model=model, input=query)
    return np.asarray([resp.data[0].embedding], dtype=np.float32)
