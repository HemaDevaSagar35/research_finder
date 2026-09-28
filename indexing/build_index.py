"""Build the hybrid index from flattened records (run flatten.py first).

Model-independent artifacts live directly in index/ and are built once per
flatten:

    bm25/              bm25s lexical index over the record texts
    metadata.sqlite    papers table (paper doc: title, venue, year, facets,
                       reproducibility flags, ...) and records table
                       (row -> record_id, paper_id, type, text, meta). Row i
                       of the records table is record i of records.jsonl,
                       which is also row i of every embedding matrix and
                       FAISS/BM25 doc i — so a hit from any retriever is one
                       SQL lookup away and filters are one SQL query.

Each embedding variant (provider/model/dim) gets its own folder so several
can be compared on the same records — index/embeddings/<slug>/ with:

    embeddings.npy     one embedding per record (row-parallel to records.jsonl),
                       written as a memmap so a 1M x 1024 corpus never has to
                       fit in RAM twice
    embedded_mask.npy  which rows are filled; a killed run resumes here
    embedding_ids.json record_id per row + model/dim; when the corpus changes
                       (re-flatten), rows for unchanged record_ids are copied
                       over and only new records are embedded
    vectors.faiss      FAISS inner-product index over normalized embeddings
    index_meta.json    provider + model + dimension; search.py reads these
                       so queries always match the vectors

index/active_embedding names the variant search.py uses by default; every
build points it at itself unless --no-activate is given.

Embedding provider/model come from EMBED_PROVIDER / EMBED_MODEL (and
EMBED_DIM) in .env or the CLI flags; see indexing/embeddings.py for options
(deepinfra for open-weight models, openai, gemini, or a local model that
needs no API key).

Usage:
    uv run python -m indexing.build_index                          # .env choice
    uv run python -m indexing.build_index --embed-provider deepinfra
    uv run python -m indexing.build_index --embed-provider local --no-activate
    uv run python -m indexing.build_index --lexical-only           # no embeddings
    uv run python -m indexing.build_index --list                   # variants
"""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import bm25s
import faiss
import numpy as np
from numpy.lib.format import open_memmap

from .embeddings import (active_embedding, embed_config, embed_dim,
                         embed_texts, embedding_slug, embeddings_root,
                         list_embeddings, set_active_embedding)

REPO_ROOT = Path(__file__).resolve().parent.parent

CHECKPOINT_EVERY = 25_000  # records embedded between mask/matrix flushes
FAISS_ADD_CHUNK = 100_000  # rows normalized + added to FAISS at a time


def load_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------------------
# embeddings
# --------------------------------------------------------------------------

def _load_cache(out_dir: Path, model: str, want_dim: int | None):
    """(record_ids, matrix mmap, mask) of a compatible existing build, or
    None."""
    ids_path = out_dir / "embedding_ids.json"
    npy_path = out_dir / "embeddings.npy"
    mask_path = out_dir / "embedded_mask.npy"
    if not (ids_path.exists() and npy_path.exists() and mask_path.exists()):
        return None
    cached = json.loads(ids_path.read_text())
    matrix = np.load(npy_path, mmap_mode="r")
    if cached.get("model") != model or (
            want_dim is not None and matrix.shape[1] != want_dim):
        print(f"Existing vectors are {cached.get('model')}@{matrix.shape[1]}, "
              f"need {model}@{want_dim or 'native'}; starting fresh.")
        return None
    mask = np.load(mask_path)
    if len(cached["record_ids"]) != matrix.shape[0] or len(mask) != matrix.shape[0]:
        print("Existing embedding files are inconsistent; starting fresh.")
        return None
    return cached["record_ids"], matrix, mask


def build_embeddings(records: list[dict], out_dir: Path,
                     provider: str, model: str,
                     want_dim: int | None) -> np.ndarray:
    """Embed every record into out_dir/embeddings.npy (memmap, row i =
    record i). Rows already embedded with the same model/dim — from an
    interrupted run, or from a previous corpus that shared record_ids — are
    reused; only the rest are sent to the provider. Progress is flushed
    every CHECKPOINT_EVERY records, so a killed run can simply be
    restarted."""
    order = [r["record_id"] for r in records]
    n = len(order)
    ids_path = out_dir / "embedding_ids.json"
    npy_path = out_dir / "embeddings.npy"
    mask_path = out_dir / "embedded_mask.npy"

    cache = _load_cache(out_dir, model, want_dim)
    if cache and cache[0] == order:
        # Same corpus: resume in place.
        _, _, mask = cache
        matrix = np.load(npy_path, mmap_mode="r+")
        print(f"Resuming: {int(mask.sum())}/{n} rows already embedded.")
    else:
        old_ids, old_matrix, old_mask = cache or (None, None, None)
        dim = want_dim or (old_matrix.shape[1] if old_matrix is not None else None)
        if dim is None:
            probe = embed_texts([records[0]["text"]], provider, model,
                                dim=None, progress=False)
            dim = int(probe.shape[1])
            print(f"Probed {provider}/{model}: native dim {dim}.")
        new_path = out_dir / "embeddings.new.npy"
        matrix = open_memmap(new_path, mode="w+", dtype=np.float32, shape=(n, dim))
        mask = np.zeros(n, dtype=bool)
        if old_ids is not None:
            pos = {rid: i for i, rid in enumerate(old_ids)}
            src = np.array([pos.get(rid, -1) for rid in order])
            keep = (src >= 0)
            keep[keep] &= old_mask[src[keep]]
            dst = np.flatnonzero(keep)
            for start in range(0, len(dst), FAISS_ADD_CHUNK):
                d = dst[start:start + FAISS_ADD_CHUNK]
                matrix[d] = old_matrix[src[d]]
            mask[dst] = True
            print(f"Corpus changed: reused {len(dst)}/{n} vectors from the "
                  f"previous build.")
            del old_matrix
        matrix.flush()
        del matrix
        os.replace(new_path, npy_path)
        np.save(mask_path, mask)
        ids_path.write_text(json.dumps({"model": model, "dim": dim,
                                        "record_ids": order}))
        matrix = np.load(npy_path, mmap_mode="r+")

    todo = np.flatnonzero(~mask)
    if len(todo):
        print(f"Embedding {len(todo)} records with {provider}/{model}"
              f"{f'@{want_dim}' if want_dim else ''} "
              f"({n - len(todo)} already done)...", flush=True)
        started = time.time()
        done = 0
        for start in range(0, len(todo), CHECKPOINT_EVERY):
            rows = todo[start:start + CHECKPOINT_EVERY]
            vectors = embed_texts([records[i]["text"] for i in rows],
                                  provider, model, dim=want_dim)
            matrix[rows] = vectors.astype(np.float32, copy=False)
            mask[rows] = True
            matrix.flush()
            np.save(mask_path, mask)
            done += len(rows)
            rate = done / max(time.time() - started, 1e-6)
            print(f"Checkpoint: {done}/{len(todo)} embedded "
                  f"({rate:.0f}/s, ~{(len(todo) - done) / max(rate, 1e-6) / 60:.0f}"
                  f" min left)", flush=True)
    else:
        print("All records already embedded.")
    return matrix


def build_faiss(matrix: np.ndarray, out_dir: Path) -> None:
    """Exact inner-product index over L2-normalised rows, filled in chunks
    so the normalised copy never exists in full."""
    index = faiss.IndexFlatIP(matrix.shape[1])
    for start in range(0, matrix.shape[0], FAISS_ADD_CHUNK):
        chunk = np.asarray(matrix[start:start + FAISS_ADD_CHUNK], dtype=np.float32)
        norms = np.linalg.norm(chunk, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        index.add(np.ascontiguousarray(chunk / norms))
    faiss.write_index(index, str(out_dir / "vectors.faiss"))
    print(f"FAISS: {index.ntotal} vectors, dim {matrix.shape[1]}.")


# --------------------------------------------------------------------------
# lexical + metadata
# --------------------------------------------------------------------------

def build_bm25(records: list[dict], out_dir: Path) -> None:
    tokens = bm25s.tokenize([r["text"] for r in records], stopwords="en")
    retriever = bm25s.BM25()
    retriever.index(tokens)
    retriever.save(str(out_dir / "bm25"))
    print(f"BM25: indexed {len(records)} records.")


PAPER_COLUMNS = [
    # (column, jsonl key, json-encode?)
    ("paper_id", "paper_id", False), ("title", "title", False),
    ("authors", "authors", True), ("venue", "venue", False),
    ("venueid", "venueid", False), ("year", "year", False),
    ("paper_type", "paper_type", False), ("identifiers", "identifiers", True),
    ("abstract", "abstract", False), ("openreview_id", "openreview_id", False),
    ("forum_url", "forum_url", False), ("folder", "folder", False),
    ("research_areas", "research_areas", True), ("keywords", "keywords", True),
    ("retrieval_tags", "retrieval_tags", True),
    ("model_names", "model_names", True), ("dataset_names", "dataset_names", True),
    ("baseline_names", "baseline_names", True), ("metric_names", "metric_names", True),
    ("hardware", "hardware", True), ("number_of_runs", "number_of_runs", False),
    ("code_available", "code_available", False), ("code_url", "code_url", False),
    ("data_available", "data_available", False), ("data_url", "data_url", False),
    ("model_checkpoints_available", "model_checkpoints_available", False),
]


def build_sqlite(papers: list[dict], records: list[dict], out_dir: Path) -> None:
    """papers: one row per paper doc (list fields JSON-encoded, queryable
    with json_each). records: rowid i = records.jsonl line i = FAISS/BM25
    doc i; text + meta so search never has to load records.jsonl."""
    db_path = out_dir / "metadata.sqlite"
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    cols = ", ".join(f"{c} {'INTEGER' if c in ('year', 'number_of_runs') else 'TEXT'}"
                     for c, _, _ in PAPER_COLUMNS)
    con.execute(f"CREATE TABLE papers ({cols}, PRIMARY KEY (paper_id))")
    con.executemany(
        f"INSERT INTO papers VALUES ({','.join('?' * len(PAPER_COLUMNS))})",
        [tuple(json.dumps(p.get(k)) if enc else p.get(k)
               for _, k, enc in PAPER_COLUMNS) for p in papers])

    con.execute("""
        CREATE TABLE records (
            row INTEGER PRIMARY KEY,
            record_id TEXT NOT NULL, paper_id TEXT NOT NULL, type TEXT NOT NULL,
            text TEXT NOT NULL, meta TEXT NOT NULL,
            source_locations TEXT
        )""")
    con.executemany(
        "INSERT INTO records VALUES (?,?,?,?,?,?,?)",
        ((i, r["record_id"], r["paper_id"], r["type"], r["text"],
          json.dumps(r.get("meta") or {}),
          json.dumps(r.get("source_locations") or []))
         for i, r in enumerate(records)))
    con.execute("CREATE INDEX records_paper ON records(paper_id)")
    con.execute("CREATE INDEX records_type ON records(type)")
    con.execute("CREATE UNIQUE INDEX records_rid ON records(record_id)")
    con.commit()
    con.close()
    print(f"SQLite: {len(papers)} papers, {len(records)} records.")


def print_variants(index_dir: Path) -> None:
    active = active_embedding(index_dir)
    slugs = list_embeddings(index_dir)
    if not slugs:
        print("No embedding variants built yet.")
        return
    for slug in slugs:
        meta = json.loads((embeddings_root(index_dir) / slug
                           / "index_meta.json").read_text())
        mark = "*" if slug == active else " "
        print(f"{mark} {slug}: dim {meta['embedding_dim']}, "
              f"{meta['num_records']} records, built {meta['built_at']}")
    print("(* = active; select another with search.py --embedding <slug>)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--index-dir", default="index",
                        help="Directory with records.jsonl (default: index/)")
    parser.add_argument("--embed-provider", default=None,
                        help="deepinfra, openai, gemini or local (default: "
                             "EMBED_PROVIDER env or openai)")
    parser.add_argument("--embed-model", default=None,
                        help="Embedding model (default: EMBED_MODEL env or "
                             "the provider's default)")
    parser.add_argument("--embed-dim", type=int, default=None,
                        help="Matryoshka output dimension (default: EMBED_DIM "
                             "env, 1024 for deepinfra, else model native)")
    parser.add_argument("--no-activate", action="store_true",
                        help="Build the variant but leave active_embedding "
                             "pointing at the current default")
    parser.add_argument("--rebuild-lexical", action="store_true",
                        help="Rebuild bm25/ and metadata.sqlite even if present")
    parser.add_argument("--lexical-only", action="store_true",
                        help="Build/refresh bm25/ + metadata.sqlite and stop "
                             "(no embedding calls)")
    parser.add_argument("--list", action="store_true",
                        help="List built embedding variants and exit")
    args = parser.parse_args()

    index_dir = REPO_ROOT / args.index_dir
    if args.list:
        print_variants(index_dir)
        return

    records = load_jsonl(index_dir / "records.jsonl")
    papers = load_jsonl(index_dir / "papers.jsonl")
    print(f"{len(records)} records, {len(papers)} papers in {index_dir}")

    # Shared, model-independent artifacts (built once per flatten)
    lexical_missing = not ((index_dir / "bm25").is_dir()
                           and (index_dir / "metadata.sqlite").exists())
    if lexical_missing or args.rebuild_lexical or args.lexical_only:
        build_bm25(records, index_dir)
        build_sqlite(papers, records, index_dir)
    else:
        print("BM25 + metadata.sqlite already present (use --rebuild-lexical "
              "after re-flattening).")
    if args.lexical_only:
        return

    provider, model = embed_config(args.embed_provider, args.embed_model)
    dim = args.embed_dim or embed_dim(provider)
    slug = embedding_slug(provider, model, dim)
    out_dir = embeddings_root(index_dir) / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Embedding variant -> {out_dir}")

    matrix = build_embeddings(records, out_dir, provider, model, dim)
    build_faiss(matrix, out_dir)

    (out_dir / "index_meta.json").write_text(json.dumps({
        "embedding_provider": provider,
        "embedding_model": model,
        "embedding_dim": int(matrix.shape[1]),
        "num_records": len(records),
        "num_papers": len(papers),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2))
    if not args.no_activate:
        set_active_embedding(index_dir, slug)
    print(f"Done: {slug}" + ("" if args.no_activate else " (now active)"))
    print_variants(index_dir)


if __name__ == "__main__":
    main()
