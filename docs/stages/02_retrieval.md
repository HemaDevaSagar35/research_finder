# Stage 2 — `retrieval`

Design reference: architecture §2.2 Initial Retrieval and §2.3 Paper-Level Reranking.

## Aim

Find the small set of papers (default 15) that the rest of the pipeline will
read in depth. The index holds *records* (problem statements, method
descriptions, findings, limitations — one record per extracted field per
paper), not whole papers, so the stage has to search records with every
planned query, decide which papers those records belong to, and merge the
per-query answers into one ranking. It is fully deterministic: no model calls,
no artifact loading.

## Inputs

Entry point: `MultiQueryRetriever.retrieve(plan, *, record_k=50, paper_k=20, rrf_k=60, filters=None, timeout=120) -> RetrievalResult`
— `research/retrieval.py:215`. Called from `research/pipeline.py:143` with
`paper_k=c.initial_papers, record_k=c.record_k, filters=c.filters, timeout=c.retrieval_timeout`.
The retriever is built once in `StageRunner.get_retriever` (`pipeline.py:117`).

| Argument | From | Contract |
|---|---|---|
| `plan: QueryPlan` | stage 1 | 1–20 unique non-blank queries. |
| `paper_k` | `PipelineConfig.initial_papers` (default 15) | Final number of papers kept. |
| `record_k` | `PipelineConfig.record_k` (default 50) | Records requested per query from the backend. |
| `rrf_k` | method default 60 | Reciprocal-rank-fusion constant. |
| `filters: RetrievalFilters` | `PipelineConfig.filters` | `types: list[str]`, `year: int \| None`; the only pre-retrieval filters both backends accept (`retrieval.py:25`). |
| `timeout` | `PipelineConfig.retrieval_timeout` (120 s) | Wall clock for all queries together. |

Backend selection (`PipelineConfig.backend`):

| Backend | Class | Config / env |
|---|---|---|
| `local` | `LocalBackend(index_dir, embedding)` → `indexing.search.LocalSearchIndex` (`retrieval.py:116`) | `index_dir`, `embedding` |
| `opensearch` | `OpenSearchBackend(client)` (`retrieval.py:138`) | `OPENSEARCH_ENDPOINT`, `OPENSEARCH_INDEX_PREFIX`, `OPENSEARCH_USER/PASSWORD` or `AWS_PROFILE/AWS_REGION` (`indexing/opensearch_index.py`) |

`PipelineConfig.concurrency` sizes the thread pool used for parallel queries.

## Logic

**Step 1 — search records, one query at a time.** Each planned query is sent
to the backend, which returns up to `record_k` records with a hybrid score
(BM25 lexical match fused with embedding similarity inside the backend). The
queries run in parallel threads under a single wall-clock timeout. If *any*
query fails or the timeout fires, the whole stage fails rather than returning
a ranking built from a subset of queries — a partial ranking would silently
bias the corpus toward whichever queries happened to succeed. (`retrieve`,
`retrieval.py:227`; backends `LocalBackend.search`, `OpenSearchBackend.search`.)

**Step 2 — turn record hits into a per-query paper ranking.** A paper that
matched with three strong records is more relevant than one that matched with
a single record, but ten weak matches should not outrank one excellent match.
The roll-up therefore sorts a paper's records by score and sums them with a
geometric decay (`score₁ + 0.5·score₂ + 0.25·score₃ …`): the best record
dominates, extra matches add diminishing support. Duplicate records (the same
`record_id` returned twice) are collapsed first. (`roll_up_records`,
`retrieval.py:93`.)

**Step 3 — fuse the per-query rankings.** Scores from different queries are
not comparable (a short query produces systematically higher BM25 scores than
a long one), so the fusion ignores scores and uses *ranks*: a paper at rank
*r* in a query's list receives `1 / (60 + r)`, and its contributions across
queries are summed (reciprocal rank fusion). The effect is that a paper that
appears near the top for several different phrasings of the topic rises above
a paper that is first for one phrasing and absent for the others — which is
exactly the "seen the area, not one phrasing" goal of stage 1. Every query
gets one equal vote per paper. (`fuse_rankings`, `retrieval.py:160`.)

**Step 4 — keep provenance, truncate.** For each surviving paper the result
records which queries matched it and at what rank (`QueryMatch`), and merges
the matching records across queries so later stages can see *what text* made
the paper relevant (`RetrievedRecord`, with the union of `source_locations`).
The list is sorted by fused score with `paper_id` as a deterministic
tie-break and cut to `paper_k`.

**Empty result = stop.** If no paper is retrieved, `run_pipeline` writes
`summary.status='empty'` and ends the run (`pipeline.py:288`); there is nothing
for the landscape to describe.

## Outputs

`RetrievalResult` — `research/retrieval.py:81`.

| Field | Contract |
|---|---|
| `queries: list[str]` | Deduplicated queries actually searched. |
| `filters: RetrievalFilters` | Filters applied. |
| `rrf_k: int` | Constant used (60). |
| `papers: list[RetrievedPaper]` | Fused top-`paper_k` list, descending `retrieval_score`. |

`RetrievedPaper` (`retrieval.py:73`): `paper_id`, `retrieval_score`,
`metadata` (title/venue/year/folder when present), `matches: list[QueryMatch]`
(query, rank, retrieval_score, rrf_contribution), `records: list[RetrievedRecord]`
(`record_id`, `type`, `text`, `meta`, `source_locations`, `matches`).

Persisted as `out/retrieval.json`.

## What later stages rely on

- `contexts` must be able to load every `paper_id` in `papers`; a paper that is in the index but whose artifact is missing fails the next stage.
- The matched `records` travel into `PaperContext.matched_records`; the landscape builder uses them to anchor evidence quotes to concrete index records.
- `paper_id` is the 12-hex corpus id (`indexing/ids.py`); `record_id` follows `docs/index_contract.md`.

## Implementation vs design

§2.3 describes a separate paper-level reranking step; in the implementation
the rerank is the RRF fusion inside this stage, and there is no LLM reranker
at this point (LLM reranking happens only in `novelty_search`).
