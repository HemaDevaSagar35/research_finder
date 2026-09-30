"""Optional ranking adapter over indexing.search for the reasoner.

Search hits are *hints* only: they re-rank evidence candidates already
derived from the loaded paper.json, they never resolve JSON paths (record
ids are per-type counters with chunk suffixes, not array paths) and never
add papers outside the thread's set.

Guards: an empty paper set returns {} without calling search (allowed_rows
treats paper_ids=[] as "no paper filter", which would search the whole
corpus). The heavy retrieval stack is imported lazily so the no-index path
never pulls in FAISS / bm25s.
"""

from __future__ import annotations

from pathlib import Path

from reasoning.evidence import tokens

EVIDENCE_TYPES = ["ablation", "failure_case", "limitation_inferred",
                  "limitation_author", "key_result", "experiment", "claim",
                  "interesting_finding", "scaling"]


class IndexUnavailable(RuntimeError):
    """Index directory missing (distinct from a corrupt index / failed load)."""


def hint_tokens(query: str, paper_ids: list[str], index_dir: Path, *,
                types: list[str] | None = None, top_k: int = 40,
                use_vector: bool = True) -> dict[str, set[str]]:
    """paper_id -> tokens of the records that matched the query, for use as
    `hints` in evidence.bundle(). Empty paper set → {} (no search call)."""
    if not paper_ids:
        return {}
    index_dir = Path(index_dir)
    if not (index_dir / "metadata.sqlite").exists():
        raise IndexUnavailable(f"no index at {index_dir}")
    from indexing.search import search      # lazy: faiss / bm25s / numpy
    papers = search(query, index_dir, top_k=top_k, use_vector=use_vector,
                    paper_ids=list(paper_ids), types=types or EVIDENCE_TYPES)
    out: dict[str, set[str]] = {}
    allowed = set(paper_ids)
    for p in papers:
        if p["paper_id"] not in allowed:       # belt and braces
            continue
        toks: set[str] = set()
        for r in p["records"]:
            toks |= tokens(r["text"])
        out[p["paper_id"]] = toks
    return out
