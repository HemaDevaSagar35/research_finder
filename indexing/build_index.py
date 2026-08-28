"""Build the hybrid index from flattened records (run flatten.py first).

Produces, inside index/:

    embeddings.npy     one embedding per record (row-parallel to records.jsonl)
    embedding_ids.json record_id per row, used as a cache: reruns only embed
                       records that are new, so growing the corpus is cheap
    vectors.faiss      FAISS inner-product index over normalized embeddings
    bm25/              bm25s lexical index over the same record texts
    metadata.sqlite    papers table (paper_id -> title, venue, authors, ...)
    index_meta.json    embedding model + dimension; search.py reads the model
                       from here so queries always match the index

Embedding provider/model come from EMBED_PROVIDER / EMBED_MODEL in .env or
the CLI flags; see indexing/embeddings.py for options (openai, gemini, or a
local model that needs no API key).

Usage:
    uv run python -m indexing.build_index
    uv run python -m indexing.build_index --embed-provider local
"""

import argparse
import json
import sqlite3
import time
from pathlib import Path

import bm25s
import faiss
import numpy as np

from .embeddings import embed_config, embed_texts

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def build_embeddings(records: list[dict], out_dir: Path,
                     provider: str, model: str) -> np.ndarray:
    """Embed records, reusing cached vectors for record_ids already embedded
    with the same model."""
    ids_path = out_dir / "embedding_ids.json"
    npy_path = out_dir / "embeddings.npy"
    cache: dict[str, np.ndarray] = {}
    if ids_path.exists() and npy_path.exists():
        cached = json.loads(ids_path.read_text())
        if cached.get("model") == model:
            old = np.load(npy_path)
            cache = {rid: old[i] for i, rid in enumerate(cached["record_ids"])}
            print(f"Embedding cache: {len(cache)} vectors reusable.")

    new = [r for r in records if r["record_id"] not in cache]
    if new:
        print(f"Embedding {len(new)} new records with {provider}/{model} "
              f"({len(records) - len(new)} cached)...")
        vectors = embed_texts([r["text"] for r in new], provider, model)
        for r, v in zip(new, vectors):
            cache[r["record_id"]] = v
    else:
        print("All records already embedded.")

    matrix = np.stack([cache[r["record_id"]] for r in records])
    np.save(npy_path, matrix)
    ids_path.write_text(json.dumps(
        {"model": model, "record_ids": [r["record_id"] for r in records]}))
    return matrix


def build_faiss(matrix: np.ndarray, out_dir: Path) -> None:
    normalized = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    index = faiss.IndexFlatIP(normalized.shape[1])
    index.add(normalized)
    faiss.write_index(index, str(out_dir / "vectors.faiss"))
    print(f"FAISS: {index.ntotal} vectors, dim {normalized.shape[1]}.")


def build_bm25(records: list[dict], out_dir: Path) -> None:
    tokens = bm25s.tokenize([r["text"] for r in records], stopwords="en")
    retriever = bm25s.BM25()
    retriever.index(tokens)
    retriever.save(str(out_dir / "bm25"))
    print(f"BM25: indexed {len(records)} records.")


def build_sqlite(papers: list[dict], out_dir: Path) -> None:
    db_path = out_dir / "metadata.sqlite"
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE papers (
            paper_id TEXT PRIMARY KEY,
            title TEXT, authors TEXT, venue TEXT, venueid TEXT, year INTEGER,
            abstract TEXT, openreview_id TEXT, forum_url TEXT, folder TEXT,
            research_areas TEXT, keywords TEXT, retrieval_tags TEXT
        )""")
    con.executemany(
        "INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(p["paper_id"], p["title"], json.dumps(p["authors"]), p["venue"],
          p["venueid"], p["year"], p["abstract"], p["openreview_id"],
          p["forum_url"], p["folder"], json.dumps(p["research_areas"]),
          json.dumps(p["keywords"]), json.dumps(p["retrieval_tags"]))
         for p in papers])
    con.commit()
    con.close()
    print(f"SQLite: {len(papers)} papers.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--index-dir", default="index",
                        help="Directory with records.jsonl (default: index/)")
    parser.add_argument("--embed-provider", default=None,
                        help="openai or gemini (default: EMBED_PROVIDER env "
                             "or openai)")
    parser.add_argument("--embed-model", default=None,
                        help="Embedding model (default: EMBED_MODEL env or "
                             "the provider's default)")
    args = parser.parse_args()

    provider, model = embed_config(args.embed_provider, args.embed_model)
    out_dir = REPO_ROOT / args.index_dir
    records = load_jsonl(out_dir / "records.jsonl")
    papers = load_jsonl(out_dir / "papers.jsonl")
    print(f"{len(records)} records, {len(papers)} papers.")

    matrix = build_embeddings(records, out_dir, provider, model)
    build_faiss(matrix, out_dir)
    build_bm25(records, out_dir)
    build_sqlite(papers, out_dir)

    (out_dir / "index_meta.json").write_text(json.dumps({
        "embedding_provider": provider,
        "embedding_model": model,
        "embedding_dim": int(matrix.shape[1]),
        "num_records": len(records),
        "num_papers": len(papers),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2))
    print(f"Done: {out_dir}")


if __name__ == "__main__":
    main()
