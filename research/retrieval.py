"""Multi-query paper retrieval, independent of API transport.

Reuse one MultiQueryRetriever per service process/event loop. Its bounded
thread pool keeps synchronous search/embedding I/O off the event loop.
Production uses OpenSearchBackend with a caller-owned OpenSearch client;
LocalBackend is a development adapter. No evidence artifacts are loaded here.
"""

import argparse
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .query_planner import QueryPlan, plan_queries


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalFilters(Contract):
    """Common, pre-retrieval filters supported by both adapters.

    Other backend-specific filters are deliberately not accepted silently.
    Natural-language constraints still require downstream reranking.
    """
    types: list[str] = Field(default_factory=list)
    year: int | None = None


class RecordHit(Contract):
    record_id: str
    type: str
    text: str
    score: float = Field(ge=0, allow_inf_nan=False)
    meta: dict[str, Any] = Field(default_factory=dict)
    source_locations: list[dict[str, Any]] = Field(default_factory=list)


class PaperHit(Contract):
    """Single-query paper score: decaying sum of hybrid record scores."""
    paper_id: str
    score: float = Field(ge=0, allow_inf_nan=False)
    metadata: dict[str, Any] = Field(default_factory=dict)
    records: list[RecordHit] = Field(default_factory=list)


class QueryMatch(Contract):
    query: str
    rank: int
    retrieval_score: float
    rrf_contribution: float


class RecordMatch(Contract):
    query: str
    retrieval_score: float


class RetrievedRecord(Contract):
    record_id: str
    type: str
    text: str
    meta: dict[str, Any] = Field(default_factory=dict)
    source_locations: list[dict[str, Any]] = Field(default_factory=list)
    matches: list[RecordMatch] = Field(default_factory=list)


class RetrievedPaper(Contract):
    paper_id: str
    retrieval_score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    matches: list[QueryMatch]
    records: list[RetrievedRecord]


class RetrievalResult(Contract):
    queries: list[str]
    filters: RetrievalFilters
    rrf_k: int
    papers: list[RetrievedPaper]


class SearchBackend(Protocol):
    def search(self, query: str, *, record_k: int,
               filters: RetrievalFilters) -> list[PaperHit]: ...


def roll_up_records(hits: list[dict]) -> list[PaperHit]:
    """Deduplicate records and apply the local backend's 1, 1/2, 1/4 rollup."""
    papers: dict[str, PaperHit] = {}
    records: dict[str, dict[str, RecordHit]] = {}
    for hit in hits:
        pid = hit["paper_id"]
        paper = papers.setdefault(pid, PaperHit(
            paper_id=pid, score=0,
            metadata={key: hit[key] for key in ("title", "venue", "year", "folder")
                      if key in hit}))
        record = RecordHit(
            record_id=hit["record_id"], type=hit["type"], text=hit["text"],
            score=hit["score"], meta=hit.get("meta") or {},
            source_locations=hit.get("source_locations") or [])
        by_id = records.setdefault(pid, {})
        if record.record_id not in by_id or record.score > by_id[record.record_id].score:
            by_id[record.record_id] = record
    for pid, paper in papers.items():
        paper.records = sorted(records[pid].values(), key=lambda r: (-r.score, r.record_id))
        paper.score = sum(r.score * 0.5 ** i for i, r in enumerate(paper.records))
    return sorted(papers.values(), key=lambda p: (-p.score, p.paper_id))


class LocalBackend:
    """Reuse one backend per local corpus; close after draining its retriever."""

    def __init__(self, index_dir: Path, *, embedding: str | None = None,
                 use_vector: bool = True):
        from indexing.search import LocalSearchIndex
        self._index = LocalSearchIndex(index_dir, embedding=embedding,
                                       use_vector=use_vector)

    def search(self, query: str, *, record_k: int,
               filters: RetrievalFilters) -> list[PaperHit]:
        papers = self._index.search(query, top_k=record_k, types=filters.types,
                                    year_min=filters.year, year_max=filters.year)
        return roll_up_records([
            {**{k: p[k] for k in ("title", "venue", "year", "folder") if k in p},
             **r, "paper_id": p["paper_id"]}
            for p in papers for r in p["records"]])

    def close(self):
        self._index.close()


class OpenSearchBackend:
    """Does not create or close the supplied client; the service owns it."""
    def __init__(self, client):
        self.client = client

    def search(self, query: str, *, record_k: int,
               filters: RetrievalFilters) -> list[PaperHit]:
        from indexing.opensearch_index import search
        hits = search(self.client, query, k=record_k,
                      types=filters.types, year=filters.year)
        return roll_up_records([{**h, "score": h["rrf_score"]} for h in hits])


class RetrievalError(RuntimeError):
    """A query failed; no partial ranking is returned."""


def _positive_int(name: str, value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def fuse_rankings(queries: list[str], rankings: list[list[PaperHit]], *,
                  rrf_k: int, paper_k: int) -> list[RetrievedPaper]:
    """One equal-weight RRF contribution per paper per unique query."""
    _positive_int("rrf_k", rrf_k)
    _positive_int("paper_k", paper_k)
    if len(queries) != len(rankings):
        raise ValueError("Each query must have one ranking")
    keys = [" ".join(q.split()).casefold() for q in queries]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("Fusion queries must be nonblank and unique")
    papers: dict[str, RetrievedPaper] = {}
    record_maps: dict[str, dict[str, RetrievedRecord]] = {}
    for query, ranking in zip(queries, rankings):
        # Defensive deduplication: repeated paper entries never add extra votes.
        seen = set()
        rank = 0
        for hit in sorted(ranking, key=lambda p: (-p.score, p.paper_id)):
            if hit.paper_id in seen:
                continue
            seen.add(hit.paper_id)
            rank += 1
            contribution = 1.0 / (rrf_k + rank)
            paper = papers.setdefault(hit.paper_id, RetrievedPaper(
                paper_id=hit.paper_id, retrieval_score=0,
                metadata=hit.metadata, matches=[], records=[]))
            paper.retrieval_score += contribution
            paper.matches.append(QueryMatch(query=query, rank=rank,
                                            retrieval_score=hit.score,
                                            rrf_contribution=contribution))
            by_id = record_maps.setdefault(hit.paper_id, {})
            seen_records = set()
            for record in hit.records:
                if record.record_id in seen_records:
                    continue
                seen_records.add(record.record_id)
                merged = by_id.setdefault(record.record_id, RetrievedRecord(
                    **record.model_dump(exclude={"score"})))
                merged.matches.append(RecordMatch(query=query, retrieval_score=record.score))
                for location in record.source_locations:
                    if location not in merged.source_locations:
                        merged.source_locations.append(location)
    result = sorted(papers.values(), key=lambda p: (-p.retrieval_score, p.paper_id))[:paper_k]
    for paper in result:
        paper.records = sorted(record_maps[paper.paper_id].values(), key=lambda r: r.record_id)
    return result


class MultiQueryRetriever:
    def __init__(self, backend: SearchBackend, *, concurrency: int = 4):
        _positive_int("concurrency", concurrency)
        self.backend = backend
        self._executor = ThreadPoolExecutor(max_workers=concurrency,
                                            thread_name_prefix="retrieval")
        self._closed = False

    async def retrieve(self, plan: QueryPlan, *, record_k: int = 50,
                       paper_k: int = 20, rrf_k: int = 60,
                       filters: RetrievalFilters | None = None,
                       timeout: float = 120) -> RetrievalResult:
        """Fuse per-query paper ranks, retaining records and query provenance.

        record_k controls backend record candidate depth, not final paper count.
        Failures and deadlines fail the request, rather than silently omitting
        queries. A timed-out/cancelled synchronous call may finish in the
        background; the shared pool still bounds active calls across requests.
        Backend network timeouts must also be configured by the service.
        """
        if self._closed:
            raise RuntimeError("retriever is closed")
        for name, value in (("record_k", record_k), ("paper_k", paper_k), ("rrf_k", rrf_k)):
            _positive_int(name, value)
        if not 0 < timeout < float("inf"):
            raise ValueError("timeout must be finite and positive")
        queries, seen = [], set()
        for query in plan.queries:
            key = " ".join(query.split()).casefold()
            if not key:
                raise ValueError("queries must not be blank")
            if key not in seen:
                queries.append(query.strip())
                seen.add(key)
        if not 1 <= len(queries) <= 20:
            raise ValueError("plan must contain 1 to 20 unique queries")
        request_filters = (filters or RetrievalFilters()).model_copy(deep=True)
        loop = asyncio.get_running_loop()

        def search(query):
            try:
                return self.backend.search(query, record_k=record_k,
                                           filters=request_filters.model_copy(deep=True))
            except Exception as exc:
                raise RetrievalError(f"Retrieval failed for query {query!r}") from exc

        futures = [loop.run_in_executor(self._executor, search, q) for q in queries]
        try:
            rankings = await asyncio.wait_for(asyncio.gather(*futures), timeout=timeout)
        finally:
            for future in futures:
                if not future.done():
                    future.cancel()
        return RetrievalResult(queries=queries, filters=request_filters, rrf_k=rrf_k,
                               papers=fuse_rankings(queries, rankings, rrf_k=rrf_k,
                                                    paper_k=paper_k))

    async def aclose(self) -> None:
        """Drain worker calls before closing a caller-owned backend client."""
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


async def _run(args):
    print("Planning search queries...", file=sys.stderr, flush=True)
    plan = await plan_queries(args.query, max_queries=args.max_queries,
                              provider=args.provider, model=args.model)
    print(f"Searching {len(plan.queries)} queries with {args.backend} backend...",
          file=sys.stderr, flush=True)
    client = None
    if args.backend == "opensearch":
        from indexing.opensearch_index import get_client
        client = get_client()
        backend = OpenSearchBackend(client)
    else:
        backend = LocalBackend(Path(args.index_dir), embedding=args.embedding,
                               use_vector=not args.no_vector)
    try:
        async with MultiQueryRetriever(backend, concurrency=args.concurrency) as retriever:
            result = await retriever.retrieve(
                plan, record_k=args.record_k, paper_k=args.papers, rrf_k=args.rrf_k,
                filters=RetrievalFilters(types=args.types or [], year=args.year),
                timeout=args.timeout)
            print(f"Retrieved {len(result.papers)} papers.", file=sys.stderr, flush=True)
            print(result.model_dump_json(indent=2), flush=True)
    finally:
        if client is not None:
            client.close()
        else:
            backend.close()


def main():
    parser = argparse.ArgumentParser(description="Expand a query and fuse hybrid paper rankings")
    parser.add_argument("query")
    parser.add_argument("--backend", choices=["local", "opensearch"], default="opensearch")
    parser.add_argument("--index-dir", default="index")
    parser.add_argument("--embedding")
    parser.add_argument("--no-vector", action="store_true", help="Local BM25-only retrieval")
    parser.add_argument("--max-queries", type=int, default=5)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--record-k", type=int, default=50)
    parser.add_argument("--papers", type=int, default=20)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--types", nargs="+")
    parser.add_argument("--year", type=int)
    args = parser.parse_args()
    if args.backend == "opensearch" and (args.no_vector or args.embedding):
        parser.error("--no-vector and --embedding apply only to --backend local")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
