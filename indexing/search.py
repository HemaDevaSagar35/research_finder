"""Hybrid search over the paper index (sanity-check CLI + library).

Embeds the query (with the exact model/dim recorded in the embedding
variant's index_meta.json), retrieves from FAISS and BM25, merges the two
ranked lists with reciprocal-rank fusion, then rolls record hits up to
papers.

Filters are resolved first, into the set of allowed record rows, and pushed
into both retrievers (FAISS IDSelector, BM25 weight mask) so top-k is taken
*within* the filter rather than filtering the top-k afterwards. Filters
cover the index contract's META fields: record type, paper_id, year range,
venue, research_areas, and any per-type record meta key (severity=high,
claim_type=empirical, expected_or_unexpected=unexpected, ...). Papers that
don't match a paper-level filter (code_available, dataset_names, ...) are
excluded via a join on the papers table.

Paper score: record RRF scores sorted descending, weighted 1, 1/2, 1/4, ...
A paper whose limitation, key result and gap all match still outranks one
with a single lucky chunk, but a paper with 40 weak record hits no longer
buries one with two strong ones (v1 summed all hits, which favoured long
papers now that there are ~136 records per paper).

Several embedding variants can be built over the same records
(build_index.py); the active one is used unless --embedding names another.

Usage:
    uv run python -m indexing.search "efficient MoE inference"
    uv run python -m indexing.search "expert caching" --papers 5 --records 3
    uv run python -m indexing.search "router collapse" --types limitation_inferred failure_case
    uv run python -m indexing.search "router collapse" --meta severity=high
    uv run python -m indexing.search "speculative decoding" --areas "Large Language Models" --year-min 2026
    uv run python -m indexing.search "speculative decoding" --paper-filter "code_available = 1"
    uv run python -m indexing.search "expert caching" --embedding local__BAAI--bge-small-en-v1.5@native
    uv run python -m indexing.search "expert caching" --no-vector   # BM25 only
"""

import argparse
import json
import sqlite3
import threading
from contextlib import closing
from collections import defaultdict
from pathlib import Path

import bm25s
import faiss
import numpy as np

from .embeddings import embed_query, resolve_embedding_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
RRF_K = 60
ROLLUP_DECAY = 0.5   # weight of the i-th best record of a paper: DECAY**i


def rrf(ranked_ids: list[int]) -> dict[int, float]:
    return {idx: 1.0 / (RRF_K + rank)
            for rank, idx in enumerate(ranked_ids, start=1)}


# --------------------------------------------------------------------------
# filters -> allowed rows
# --------------------------------------------------------------------------

def allowed_rows(con: sqlite3.Connection, *,
                 types: list[str] | None = None,
                 paper_ids: list[str] | None = None,
                 year_min: int | None = None, year_max: int | None = None,
                 venue: str | None = None,
                 areas: list[str] | None = None,
                 meta: dict[str, str] | None = None,
                 paper_filter: str | None = None) -> np.ndarray | None:
    """Rows of the records table matching every given filter, or None when
    no filter is set (= everything allowed). `meta` matches record meta
    keys exactly (values compared as strings, so severity=high and
    novel_component=true both work). `paper_filter` is a raw SQL fragment
    over the papers table (aliased p) for anything else."""
    where, params = [], []
    if types:
        where.append(f"r.type IN ({','.join('?' * len(types))})")
        params += types
    if paper_ids:
        where.append(f"r.paper_id IN ({','.join('?' * len(paper_ids))})")
        params += paper_ids
    if year_min is not None:
        where.append("CAST(json_extract(r.meta, '$.year') AS INTEGER) >= ?")
        params.append(year_min)
    if year_max is not None:
        where.append("CAST(json_extract(r.meta, '$.year') AS INTEGER) <= ?")
        params.append(year_max)
    if venue:
        where.append("lower(json_extract(r.meta, '$.venue')) LIKE ?")
        params.append(f"%{venue.lower()}%")
    for area in areas or []:
        where.append("EXISTS (SELECT 1 FROM json_each(r.meta, '$.research_areas') a "
                     "WHERE lower(a.value) = ?)")
        params.append(area.lower())
    for key, value in (meta or {}).items():
        if not key.replace("_", "").isalnum():
            raise ValueError(f"bad meta key {key!r}")
        where.append(f"lower(CAST(json_extract(r.meta, '$.{key}') AS TEXT)) = ?")
        params.append(str(value).lower())
    join = ""
    if paper_filter:
        join = "JOIN papers p ON p.paper_id = r.paper_id"
        where.append(f"({paper_filter})")
    if not where:
        return None
    sql = f"SELECT r.row FROM records r {join} WHERE {' AND '.join(where)}"
    rows = np.fromiter((row for (row,) in con.execute(sql, params)), dtype=np.int64)
    return rows


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

class LocalSearchIndex:
    """Reusable read-only FAISS/BM25 indexes with request-local SQLite access.

    Loading is lazy and synchronized; concurrent queries share one allocation.
    Keep one instance per corpus/embedding configuration for the service lifetime.
    The embedding variant is pinned on first vector use. Do not rebuild index
    files in place while this instance is live; drain searches and create a new
    instance after publishing a new index. close() also requires drained searches.
    """

    def __init__(self, index_dir: Path, *, embedding: str | None = None,
                 use_vector: bool = True, use_bm25: bool = True):
        self.index_dir = Path(index_dir).resolve()
        self.embedding = embedding
        self.use_vector = use_vector
        self.use_bm25 = use_bm25
        self._load_lock = threading.Lock()
        self._vector = None
        self._lexical = None
        self._closed = False

    def _check_open(self):
        if self._closed:
            raise RuntimeError("local search index is closed")

    def _vector_index(self):
        with self._load_lock:
            self._check_open()
            if self._vector is None:
                emb_dir = resolve_embedding_dir(self.index_dir, self.embedding)
                meta = json.loads((emb_dir / "index_meta.json").read_text())
                index = faiss.read_index(str(emb_dir / "vectors.faiss"), faiss.IO_FLAG_MMAP)
                # Publish only after a complete load; failed loads can be retried.
                self._vector = (index, meta)
            return self._vector

    def _lexical_index(self):
        with self._load_lock:
            self._check_open()
            if self._lexical is None:
                self._lexical = bm25s.BM25.load(
                    str(self.index_dir / "bm25"), load_corpus=False, mmap=True)
            return self._lexical

    def close(self):
        """Release index references after all searches have finished."""
        with self._load_lock:
            self._closed = True
            self._vector = None
            self._lexical = None

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *args):
        self.close()

    def search(self, query: str, top_k: int = 50, **filters) -> list[dict]:
        """Return ranked papers and matching records, including evidence pointers."""
        self._check_open()
        database_uri = (self.index_dir / "metadata.sqlite").as_uri() + "?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True)) as con:
            n_records = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if n_records == 0:
                return []
            rows = allowed_rows(con, **filters)
            if rows is not None and len(rows) == 0:
                return []
            ranked_lists: list[list[int]] = []

            if self.use_vector:
                index, meta = self._vector_index()
                # Must use the exact provider/model/dim that built the vectors
                qvec = embed_query(query, meta.get("embedding_provider", "openai"),
                                   meta["embedding_model"], meta.get("embedding_dim"))
                qvec = qvec.reshape(1, -1).astype(np.float32)
                qvec /= np.linalg.norm(qvec)
                k = min(top_k, index.ntotal if rows is None else len(rows))
                params = None
                if rows is not None:
                    params = faiss.SearchParameters(sel=faiss.IDSelectorBatch(rows))
                _, vec_ids = index.search(qvec, k, params=params)
                ranked_lists.append([int(i) for i in vec_ids[0] if i >= 0])

            if self.use_bm25:
                retriever = self._lexical_index()
                tokens = bm25s.tokenize(query, stopwords="en", show_progress=False)
                mask = None
                if rows is not None:
                    mask = np.zeros(n_records, dtype=np.float32)
                    mask[rows] = 1.0
                k = min(top_k, n_records if rows is None else len(rows))
                bm_ids, bm_scores = retriever.retrieve(
                    tokens, k=k, show_progress=False, weight_mask=mask)
                ranked_lists.append(
                    [int(i) for i, s in zip(bm_ids[0], bm_scores[0]) if s > 0])

            # Fuse
            fused: dict[int, float] = defaultdict(float)
            for ranked in ranked_lists:
                for idx, score in rrf(ranked).items():
                    fused[idx] += score
            if not fused:
                return []

            # Fetch hit records and roll up to papers
            hit_rows = list(fused)
            by_row = {}
            for start in range(0, len(hit_rows), 900):   # SQLite variable limit
                chunk = hit_rows[start:start + 900]
                for row, rid, pid, rtype, text, rmeta, locations in con.execute(
                        f"SELECT row, record_id, paper_id, type, text, meta, source_locations FROM records "
                        f"WHERE row IN ({','.join('?' * len(chunk))})", chunk):
                    by_row[row] = {"record_id": rid, "paper_id": pid, "type": rtype,
                                   "text": text, "meta": json.loads(rmeta),
                                   "source_locations": json.loads(locations or "[]")}

            papers: dict[str, dict] = {}
            for idx, score in fused.items():
                record = by_row[idx]
                paper = papers.setdefault(record["paper_id"],
                                          {"paper_id": record["paper_id"],
                                           "score": 0.0, "records": []})
                paper["records"].append({"score": score, **record})

            for paper in papers.values():
                paper["records"].sort(key=lambda r: -r["score"])
                paper["score"] = sum(r["score"] * ROLLUP_DECAY ** i
                                     for i, r in enumerate(paper["records"]))
                paper["matched_types"] = sorted({r["type"] for r in paper["records"]})

            pids = list(papers)
            for start in range(0, len(pids), 900):
                chunk = pids[start:start + 900]
                for pid, title, venue, year, folder in con.execute(
                        f"SELECT paper_id, title, venue, year, folder FROM papers "
                        f"WHERE paper_id IN ({','.join('?' * len(chunk))})", chunk):
                    papers[pid].update(title=title, venue=venue, year=year, folder=folder)
            return sorted(papers.values(), key=lambda p: -p["score"])


def search(query: str, index_dir: Path, top_k: int = 50,
           embedding: str | None = None, use_vector: bool = True,
           use_bm25: bool = True, **filters) -> list[dict]:
    """One-shot search; reuse LocalSearchIndex for multiple queries.

    Filters are those of allowed_rows(). Returns paper metadata, decaying
    record RRF scores, and matching records with source_locations.
    """
    with LocalSearchIndex(index_dir, embedding=embedding, use_vector=use_vector,
                          use_bm25=use_bm25) as index:
        return index.search(query, top_k=top_k, **filters)


def _parse_meta(pairs: list[str] | None) -> dict[str, str]:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--meta expects key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


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
    parser.add_argument("--embedding", default=None,
                        help="Embedding variant slug under index/embeddings/ "
                             "(default: the active one; see build_index --list)")
    parser.add_argument("--no-vector", action="store_true", help="BM25 only")
    parser.add_argument("--no-bm25", action="store_true", help="Vector only")
    f = parser.add_argument_group("filters (record META from the index contract)")
    f.add_argument("--types", nargs="+", metavar="TYPE",
                   help="Record types, e.g. hypothesis limitation_inferred")
    f.add_argument("--paper-ids", nargs="+", metavar="ID",
                   help="Restrict to these papers (second-pass RAG)")
    f.add_argument("--year-min", type=int)
    f.add_argument("--year-max", type=int)
    f.add_argument("--venue", help="Substring match, e.g. ICLR")
    f.add_argument("--areas", nargs="+", metavar="AREA",
                   help="research_areas values (exact, case-insensitive)")
    f.add_argument("--meta", nargs="+", metavar="KEY=VALUE",
                   help="Record meta equality, e.g. severity=high "
                        "claim_type=empirical expected_or_unexpected=unexpected")
    f.add_argument("--paper-filter", metavar="SQL",
                   help="Raw SQL over the papers table as p, e.g. "
                        "\"p.code_available = 1\" or "
                        "\"EXISTS (SELECT 1 FROM json_each(p.dataset_names) "
                        "WHERE value = 'GSM-8K')\"")
    parser.add_argument("--json", action="store_true",
                        help="Print results as JSON instead of text")
    args = parser.parse_args()
    if args.no_vector and args.no_bm25:
        parser.error("--no-vector and --no-bm25 together leave no retriever")

    index_dir = REPO_ROOT / args.index_dir
    results = search(args.query, index_dir, top_k=args.top_k,
                     embedding=args.embedding, use_vector=not args.no_vector,
                     use_bm25=not args.no_bm25,
                     types=args.types, paper_ids=args.paper_ids,
                     year_min=args.year_min, year_max=args.year_max,
                     venue=args.venue, areas=args.areas,
                     meta=_parse_meta(args.meta), paper_filter=args.paper_filter)

    if args.json:
        print(json.dumps(results[:args.papers], indent=1, ensure_ascii=False))
        return
    if not results:
        print("No results (filters may have excluded everything).")
        return
    for i, paper in enumerate(results[:args.papers], start=1):
        print(f"\n{i}. [{paper['score']:.3f}] {paper.get('title', '?')} "
              f"({paper.get('venue', '?')} {paper.get('year') or ''})  "
              f"id={paper['paper_id']}  hits={len(paper['records'])}")
        for record in paper["records"][:args.records]:
            text = record["text"]
            text = text if len(text) <= 220 else text[:220] + "..."
            flags = {k: v for k, v in record["meta"].items()
                     if k not in ("year", "venue", "research_areas")}
            print(f"     - {record['type']}{f' {flags}' if flags else ''}: {text}")


if __name__ == "__main__":
    main()
