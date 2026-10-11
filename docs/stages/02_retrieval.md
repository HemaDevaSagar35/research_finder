# Stage 2 — `retrieval`

Design reference: architecture §2.2 Initial Retrieval and §2.3 Paper-Level Reranking.

## Aim

Run every planned query against the paper index, fuse the per-query paper
rankings into one list, and keep the matching records with their provenance.
Purely deterministic: no model calls, no artifact loading.

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

1. Validate integers, finite timeout, and the query list (`retrieval.py:227`).
2. Search each query in the thread pool: `backend.search(query, record_k, filters)`. Any backend exception → `RetrievalError`; `asyncio.wait_for` timeout fails the whole stage. No partial rankings are accepted (`retrieval.py:246`).
3. Each backend returns record hits; `roll_up_records` (`retrieval.py:93`) dedups records by `record_id`, scores a paper as `Σ score·0.5^i` over its records sorted by score, and sorts papers by `(-score, paper_id)`.
4. `fuse_rankings` (`retrieval.py:160`): per query, rank papers and add `1/(rrf_k + rank)` to the paper's `retrieval_score`; record a `QueryMatch` per query; merge records across queries by `record_id` (union of `source_locations`, one `RecordMatch` per query). Sort by `(-retrieval_score, paper_id)`, truncate to `paper_k`.
5. Return `RetrievalResult`.

`run_pipeline` then checks `state['retrieval'].papers`; if empty the run stops
with `summary.status='empty'` (`pipeline.py:288`).

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

- `contexts` iterates `papers` and must be able to load every `paper_id`.
- `records` are carried into `PaperContext.matched_records` and used by the landscape builder to resolve evidence quotes to records.
- `paper_id` is the 12-hex corpus id (`indexing/ids.py`), `record_id` follows `docs/index_contract.md`.

## Implementation vs design

§2.3 describes a separate paper-level reranking step; in the implementation
the rerank is the RRF fusion inside this stage, and there is no LLM reranker
at this point (LLM reranking happens only in `novelty_search`).
