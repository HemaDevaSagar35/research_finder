"""Embedding backends for the index: DeepInfra (open-weight models), OpenAI,
Gemini, or a local model.

Provider/model come from EMBED_PROVIDER / EMBED_MODEL in .env (or function
arguments). Defaults per provider:

    deepinfra  Qwen/Qwen3-Embedding-8B  (needs DEEPINFRA_API_KEY; $0.01/M tok)
    openai     text-embedding-3-small   (needs OPENAI_API_KEY)
    gemini     gemini-embedding-001     (needs GEMINI_API_KEY)
    local      BAAI/bge-small-en-v1.5   (no key; fastembed downloads the ONNX
                                         model on first use, ~130 MB)

EMBED_DIM truncates Matryoshka-trained models (Qwen3, OpenAI v3, Gemini) to
that many dimensions server-side; default 1024 for deepinfra, model default
otherwise. 500k records x 4096 float32 would be 8 GB, so keep this modest.

Qwen3-Embedding is asymmetric: queries are prefixed with a task instruction
("Instruct: ...\\nQuery: ..."), documents are embedded as-is. embed_query()
applies that prefix automatically for Qwen models (EMBED_QUERY_INSTRUCTION
overrides the instruction text).

API batches run in parallel (EMBED_CONCURRENCY, default 8 threads); the SDK
retries transient failures per request.

Several embeddings can coexist for the same records: build_index writes each
provider/model/dim variant to index/embeddings/<slug>/ (see embedding_slug)
and points index/active_embedding at the latest build; search.py uses the
active one unless --embedding <slug> says otherwise. Queries must be embedded
with the same provider/model/dim that built the vectors, which is why every
variant carries its own index_meta.json.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()  # EMBED_* and provider API keys; before CONCURRENCY is read

DEFAULT_MODELS = {
    "deepinfra": "Qwen/Qwen3-Embedding-8B",
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-001",
    "local": "BAAI/bge-small-en-v1.5",
}

DEFAULT_DIMS = {"deepinfra": 1024}

API_BATCH = 100  # Gemini's OpenAI-compat endpoint caps batches at 100
CONCURRENCY = int(os.environ.get("EMBED_CONCURRENCY", "8"))

QWEN_QUERY_INSTRUCTION = os.environ.get(
    "EMBED_QUERY_INSTRUCTION",
    "Given a research question about machine learning, retrieve passages "
    "from papers that answer it")


def embed_config(provider: str | None = None,
                 model: str | None = None) -> tuple[str, str]:
    provider = (provider or os.environ.get("EMBED_PROVIDER") or "openai").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unsupported embedding provider {provider!r}; "
                         f"known: {list(DEFAULT_MODELS)}")
    model = model or os.environ.get("EMBED_MODEL") or DEFAULT_MODELS[provider]
    return provider, model


def embed_dim(provider: str) -> int | None:
    """Requested output dimension (None = the model's native size)."""
    if provider == "local":
        return None
    env = os.environ.get("EMBED_DIM")
    if env:
        return int(env)
    return DEFAULT_DIMS.get(provider)


def embedding_slug(provider: str, model: str, dim: int | None) -> str:
    """Folder name for one embedding variant, e.g.
    deepinfra__Qwen--Qwen3-Embedding-8B@1024 or local__BAAI--bge-small-en-v1.5@native."""
    return f"{provider}__{model.replace('/', '--')}@{dim or 'native'}"


def embeddings_root(index_dir: Path) -> Path:
    return index_dir / "embeddings"


def list_embeddings(index_dir: Path) -> list[str]:
    root = embeddings_root(index_dir)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if (p / "index_meta.json").exists())


def active_embedding(index_dir: Path) -> str | None:
    marker = index_dir / "active_embedding"
    return marker.read_text().strip() if marker.exists() else None


def set_active_embedding(index_dir: Path, slug: str) -> None:
    (index_dir / "active_embedding").write_text(slug + "\n")


def resolve_embedding_dir(index_dir: Path, slug: str | None = None) -> Path:
    """Directory of the requested (or active, or only) embedding variant."""
    available = list_embeddings(index_dir)
    if slug is None:
        slug = active_embedding(index_dir)
        if slug is None and len(available) == 1:
            slug = available[0]
    if slug is None:
        raise SystemExit(
            "No embedding selected. Build one with indexing.build_index or "
            f"pass --embedding <slug>. Available: {available or 'none'}")
    path = embeddings_root(index_dir) / slug
    if not (path / "index_meta.json").exists():
        raise SystemExit(f"Unknown embedding {slug!r}. Available: {available}")
    return path


def _local_model(model: str):
    from fastembed import TextEmbedding
    return TextEmbedding(model)


def _api_client(provider: str):
    from llm_client import LLMClient
    return LLMClient(provider).raw


def _api_kwargs(model: str, dim: int | None) -> dict:
    return {"model": model, **({"dimensions": dim} if dim else {})}


def _is_qwen(model: str) -> bool:
    return "qwen" in model.lower()


def embed_texts(texts: list[str], provider: str, model: str,
                dim: int | None = None, progress: bool = True) -> np.ndarray:
    """Embed documents/records. dim=None means embed_dim(provider)."""
    if provider == "local":
        vectors = list(_local_model(model).embed(texts, batch_size=64))
        return np.asarray(vectors, dtype=np.float32)
    dim = dim or embed_dim(provider)
    if not texts:
        return np.zeros((0, dim or 0), dtype=np.float32)
    client = _api_client(provider)
    kwargs = _api_kwargs(model, dim)
    batches = [texts[i:i + API_BATCH] for i in range(0, len(texts), API_BATCH)]
    done = 0

    def run(batch: list[str]) -> list[list[float]]:
        resp = client.embeddings.create(input=batch, **kwargs)
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]

    vectors: list[list[float]] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for i, result in enumerate(pool.map(run, batches)):
            vectors.extend(result)
            done += len(batches[i])
            if progress and (i % 20 == 19 or done == len(texts)):
                print(f"  embedded {done}/{len(texts)}", flush=True)
    return np.asarray(vectors, dtype=np.float32)


def embed_query(query: str, provider: str, model: str,
                dim: int | None = None) -> np.ndarray:
    """Embed a search query (asymmetric models get their query prefix).
    Pass the dim recorded in the index meta so it matches the vectors."""
    if provider == "local":
        return np.asarray(list(_local_model(model).query_embed(query)),
                          dtype=np.float32)
    if _is_qwen(model):
        query = f"Instruct: {QWEN_QUERY_INSTRUCTION}\nQuery: {query}"
    client = _api_client(provider)
    resp = client.embeddings.create(
        input=query, **_api_kwargs(model, dim or embed_dim(provider)))
    return np.asarray([resp.data[0].embedding], dtype=np.float32)
