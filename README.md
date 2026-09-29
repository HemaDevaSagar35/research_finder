# research_finder

Tools for finding and downloading research papers.

- `scrapper/` — downloads accepted papers (e.g. ICML 2026 spotlights) from
  OpenReview. See `scrapper/README.md`.
- `extraction/` — converts PDF pages (e.g. downloaded papers) to Markdown
  using a vision LLM via `llm_client`, then turns a paper's Markdown pages
  into a single machine-readable JSON record (for RAG):

  ```bash
  uv run python -m extraction.pdf_to_markdown paper.pdf --out-dir markdown
  uv run python -m extraction.research_extract "markdown/<paper_folder>"
  ```
- `indexing/` — flattens extracted `paper.json` files into typed retrieval
  records and builds a local hybrid index (FAISS vectors + BM25 + SQLite
  metadata). See `docs/offline_ingestion_design.md`:

  ```bash
  uv run python -m indexing.flatten --roots markdown
  uv run python -m indexing.build_index
  uv run python -m indexing.search "efficient MoE inference"
  ```
- `llm_client/` — unified client for OpenAI, Gemini, and DeepSeek chat APIs.
  Set `OPENAI_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY`, then:

  ```python
  from llm_client import chat
  print(chat("Say hi in one word.", model="gemini-2.5-flash"))
  ```

## Query planning

Expand a research query into complementary search queries:

```bash
uv run python -m research.query_planner "efficient MoE inference" --max-queries 5
```

Returns only `{"queries": [...]}`, with the original query first. Configure
`QUERY_PLANNER_PROVIDER` / `QUERY_PLANNER_MODEL`, or use the existing provider
settings; CLI overrides are `--provider` / `--model`. Requires a chat model.
Multi-query retrieval is implemented below; reranking and hypothesis generation
remain planned.

For the server, call `await research.query_planner.plan_queries(query,
client=shared_client)` with a shared `AsyncLLMClient`. The caller owns a supplied
client; temporary clients are closed automatically. The result is a `QueryPlan`
Pydantic model (`plan.model_dump()` produces the JSON-compatible dictionary).

Run offline planner and retrieval tests with `uv run python -m unittest discover -s tests -v`.

## Multi-query retrieval

Expand a topic and retrieve fused paper rankings from OpenSearch:

```bash
uv run python -m research.retrieval "efficient MoE inference" --papers 20 --max-queries 5
# Development index, lexical retrieval only (planning still requires a chat model):
uv run python -m research.retrieval "efficient MoE inference" --backend local --no-vector --index-dir index
```

Progress is printed to stderr; stdout contains a JSON `RetrievalResult`: deduplicated queries, applied filters,
ranked papers, per-query ranks/scores, matching records, and source locations.
Each query contributes one reciprocal-rank vote per paper; the final
`retrieval_score` is a ranking signal, not a relevance probability.
`--types` and exact `--year` filters apply to every search. `--record-k`
controls backend record candidate depth (local: per search channel;
OpenSearch: fused records), while `--papers` caps the final paper count.

The entire query-to-hypothesis pipeline must remain deployable as one server
service, per `docs/research_path_generator_architecture_refined.md`. Production
uses OpenSearch and the future S3 evidence loader; local artifacts are for
development. API hosting, admission control, and downstream generation stages
are still pending.

Within each query, unique record scores roll up with weights 1, 1/2, 1/4, ... .
Across queries, papers receive `sum(1 / (rrf_k + paper_rank))`, with equal query
weights and configurable `--rrf-k` (default 60). Score ties use paper IDs.
Natural-language constraints are not guaranteed by retrieval; downstream
reranking must check them. Broader backend-specific filters are not exposed.

For service integration, reuse one retriever per process/event loop:

```python
from research.query_planner import QueryPlan
from research.retrieval import MultiQueryRetriever, OpenSearchBackend, RetrievalFilters

# search_client is a caller-owned OpenSearch client.
async with MultiQueryRetriever(OpenSearchBackend(search_client), concurrency=4) as retriever:
    result = await retriever.retrieve(
        QueryPlan(queries=["efficient MoE inference", "expert caching"]),
        filters=RetrievalFilters(year=2026), paper_k=20, timeout=120,
    )
```

In a server, keep that context open for the service lifetime. Close the
retriever before closing its backend client. Blocking search runs in a bounded
thread pool. Both backends honor `--concurrency` (default 4). Each local backend
lazily loads one FAISS index and one memory-mapped BM25 index, then shares them
across concurrent queries and subsequent requests. Filters and SQLite connections
remain per search. A query failure fails the request without a partial ranking.
Timeouts cancel queued work, but running synchronous calls finish in the
background, so configure backend network timeouts too. This stage returns
indexed evidence pointers; evidence loading and reranking are separate future
stages. OpenSearch adapter behavior has offline test coverage; live retrieval
quality has not been evaluated.

For local service use, create one `LocalBackend(Path("index"))` per corpus and
embedding configuration and reuse it with the retriever. After awaiting
`retriever.aclose()`, call `backend.close()` to release index references. The CLI
does this automatically. Separate backend instances or processes each own their
own index copy. The embedding variant stays pinned after first use; drain and
recreate the backend after rebuilding or switching indexes, and do not modify
index files while searches are active. The one-shot `indexing.search.search()`
API remains available; repeated callers should reuse `LocalSearchIndex` instead.

## Setup

Requires [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```
