"""Hybrid search over the paper index (sanity-check CLI).

Embeds the query (with the exact model recorded in index_meta.json), retrieves
from FAISS and BM25, merges the two ranked lists with reciprocal-rank fusion,
then rolls record hits up to papers: a paper whose limitation, key result, and
gap all match ranks above one with a single lucky chunk.

Usage:
    uv run python -m indexing.search "efficient MoE inference"
    uv run python -m indexing.search "expert caching" --papers 5 --records 3
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import bm25s
import faiss
import numpy as np

from .embeddings import embed_query

REPO_ROOT = Path(__file__).resolve().parent.parent
RRF_K = 60


def rrf(ranked_ids: list[int]) -> dict[int, float]:
    return {idx: 1.0 / (RRF_K + rank)
            for rank, idx in enumerate(ranked_ids, start=1)}


def search(query: str, index_dir: Path, top_k: int = 50) -> list[dict]:
    """Return papers ranked by fused record-level scores."""
    meta = json.loads((index_dir / "index_meta.json").read_text())
    with open(index_dir / "records.jsonl") as f:
        records = [json.loads(line) for line in f if line.strip()]

    # Vector side (must use the exact provider/model that built the index)
    qvec = embed_query(query, meta.get("embedding_provider", "openai"),
                       meta["embedding_model"])
    qvec = qvec.reshape(1, -1)
    qvec /= np.linalg.norm(qvec)
    index = faiss.read_index(str(index_dir / "vectors.faiss"))
    _, vec_ids = index.search(qvec, min(top_k, index.ntotal))

    # Lexical side
    retriever = bm25s.BM25.load(str(index_dir / "bm25"), load_corpus=False)
    tokens = bm25s.tokenize(query, stopwords="en", show_progress=False)
    bm_ids, bm_scores = retriever.retrieve(
        tokens, k=min(top_k, len(records)), show_progress=False)
    bm_ranked = [int(i) for i, s in zip(bm_ids[0], bm_scores[0]) if s > 0]

    # Fuse and roll up to papers
    fused: dict[int, float] = defaultdict(float)
    for scores in (rrf([int(i) for i in vec_ids[0]]), rrf(bm_ranked)):
        for idx, score in scores.items():
            fused[idx] += score

    papers: dict[str, dict] = {}
    for idx, score in fused.items():
        record = records[idx]
        paper = papers.setdefault(record["paper_id"],
                                  {"paper_id": record["paper_id"],
                                   "score": 0.0, "records": []})
        paper["score"] += score
        paper["records"].append({"score": score, **record})

    for paper in papers.values():
        paper["records"].sort(key=lambda r: -r["score"])
    return sorted(papers.values(), key=lambda p: -p["score"])


def paper_titles(index_dir: Path) -> dict[str, dict]:
    con = sqlite3.connect(index_dir / "metadata.sqlite")
    rows = con.execute(
        "SELECT paper_id, title, venue, folder FROM papers").fetchall()
    con.close()
    return {r[0]: {"title": r[1], "venue": r[2], "folder": r[3]} for r in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("query")
    parser.add_argument("--index-dir", default="index")
    parser.add_argument("--papers", type=int, default=10,
                        help="Papers to show (default: 10)")
    parser.add_argument("--records", type=int, default=3,
                        help="Matching records to show per paper (default: 3)")
    parser.add_argument("--top-k", type=int, default=50,
                        help="Records fetched per retriever before fusion")
    args = parser.parse_args()

    index_dir = REPO_ROOT / args.index_dir
    results = search(args.query, index_dir, top_k=args.top_k)
    info = paper_titles(index_dir)

    for i, paper in enumerate(results[:args.papers], start=1):
        detail = info.get(paper["paper_id"], {})
        print(f"\n{i}. [{paper['score']:.3f}] {detail.get('title', '?')} "
              f"({detail.get('venue', '?')})  id={paper['paper_id']}")
        for record in paper["records"][:args.records]:
            text = record["text"]
            text = text if len(text) <= 220 else text[:220] + "..."
            print(f"     - {record['type']}: {text}")


if __name__ == "__main__":
    main()
