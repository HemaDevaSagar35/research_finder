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
- `reasoning/` — query-time Cross-Paper Reasoner: takes a Research Landscape
  plus the `paper.json` / page artifacts behind it and emits evidence-grounded,
  page-reviewed cross-paper findings, observations and tensions (plus typed
  diagnostics). See `docs/cross_paper_reasoner.md`:

  ```bash
  uv run python -m reasoning.fixtures --out /tmp/demo          # synthetic corpus
  uv run python -m reasoning.cross_paper --landscape /tmp/demo/landscape.json \
      --root /tmp/demo --chat fake --no-s3 --out /tmp/out.json  # offline smoke run
  uv run pytest tests/reasoning
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

### Combined output contract (for landscape contributors)

`await retriever.retrieve(plan, ...)` returns one `RetrievalResult` combining
**all queries**, using the same schema for local and OpenSearch backends. The
CLI emits `result.model_dump_json(indent=2)` to stdout. In Python, use
`result.model_dump(mode="json")` for a JSON-compatible dictionary. The source
of truth is the Pydantic models in [`research/retrieval.py`](research/retrieval.py);
`RetrievalResult.model_json_schema()` produces the machine-readable JSON Schema.

This illustrative result contains one paper matched by two queries. IDs,
metadata, text, and scores below are examples, not findings from a real paper.

```json
{
  "queries": ["efficient MoE inference", "expert caching"],
  "filters": {"types": [], "year": null},
  "rrf_k": 60,
  "papers": [
    {
      "paper_id": "example-paper-id",
      "retrieval_score": 0.03278688524590164,
      "metadata": {
        "title": "Example paper on expert caching",
        "venue": "ICML",
        "year": 2026,
        "folder": "example-paper-folder"
      },
      "matches": [
        {
          "query": "efficient MoE inference",
          "rank": 1,
          "retrieval_score": 0.02,
          "rrf_contribution": 0.01639344262295082
        },
        {
          "query": "expert caching",
          "rank": 1,
          "retrieval_score": 0.018,
          "rrf_contribution": 0.01639344262295082
        }
      ],
      "records": [
        {
          "record_id": "example-record-id",
          "type": "key_result",
          "text": "Result: Expert caching reduced inference latency in the evaluated setting.",
          "meta": {"year": 2026, "venue": "ICML"},
          "source_locations": [
            {
              "page": 3,
              "section": "Experiments",
              "table": "Table 1",
              "figure": null,
              "equation": null,
              "appendix": null
            }
          ],
          "matches": [
            {"query": "efficient MoE inference", "retrieval_score": 0.02},
            {"query": "expert caching", "retrieval_score": 0.018}
          ]
        }
      ]
    }
  ]
}
```

Array notation (`[]`) below means “each element.” Model fields are shown in
full in the example; keys within free-form dictionaries may be absent or null.

| Field | JSON type | Meaning |
|---|---|---|
| `queries` | array of strings | Actual deduplicated queries searched, in plan order. The planner puts the original user query first. |
| `filters.types` | array of strings | Allowed record types; `[]` means no type restriction. |
| `filters.year` | integer or null | Exact year restriction; `null` means unrestricted. |
| `rrf_k` | integer | Smoothing constant for cross-query paper fusion; default `60`. |
| `papers` | array of objects | Final unique papers, ranked by descending fused score; ties use ascending paper ID. Can be empty. |
| `papers[].paper_id` | string | Paper identifier for downstream artifact lookup. Treat it as opaque. |
| `papers[].retrieval_score` | number | Final score: sum of this paper's `matches[].rrf_contribution`. Not a confidence or novelty score. |
| `papers[].metadata` | object | Available paper fields (`title`, `venue`, `year`, `folder`); completeness depends on the index/backend. Can be `{}`. |
| `papers[].matches` | array of objects | One entry per query that retrieved this paper, in query order. Missing queries contribute zero. |
| `papers[].matches[].query` | string | Originating search, matching an entry in top-level `queries`. |
| `papers[].matches[].rank` | integer | Paper's 1-based rank within that query, before the final paper cap. |
| `papers[].matches[].retrieval_score` | number | That query's paper score: descending record scores weighted `1, 1/2, 1/4, ...`. |
| `papers[].matches[].rrf_contribution` | number | `1 / (rrf_k + rank)` added to the final paper score. |
| `papers[].records` | array of objects | Matching indexed records, deduplicated by record ID within this paper across queries. Sorted by record ID, **not relevance**. |
| `papers[].records[].record_id` | string | Opaque indexed record/chunk identifier; preserve it for provenance. |
| `papers[].records[].type` | string | Flattened extraction record type, such as `key_result` or `limitation_inferred`; see the [index contract](docs/index_contract.md). |
| `papers[].records[].text` | string | Indexed text derived from structured extraction, possibly chunked; not necessarily a verbatim paper quotation. |
| `papers[].records[].meta` | object | Record-specific metadata passed through from the index. Keys vary by record type; can be `{}`. |
| `papers[].records[].source_locations` | array of objects | Evidence pointers merged across queries. May be empty; location fields can be null. |
| `papers[].records[].matches` | array of objects | Queries that retrieved this specific record, in query order. Can be a subset of the paper's matching queries. |
| `papers[].records[].matches[].query` | string | Originating search for the record. |
| `papers[].records[].matches[].retrieval_score` | number | Record's within-query hybrid BM25/vector RRF score, before paper rollup. |

Source locations are passed through as dictionaries. Current extraction emits
`page` (integer or null) and `section`, `table`, `figure`, `equation`, `appendix`
(strings or null), as defined by `SourceLocation` in
[`extraction/research_extract.py`](extraction/research_extract.py). Page numbers
refer to the extraction's explicit page identifiers. Retrieval does not load
or verify the referenced page content.

**Limits and completion:** defaults are up to 5 planned queries, `record_k=50`,
and `paper_k=20`. Local hybrid retrieval takes up to 50 records per channel
(up to 100 unique records per query); OpenSearch returns up to 50 fused records
per query. There is no separate per-query paper cap or per-paper record cap.
The final 20-paper cap applies after combining all queries. Fewer papers can be
returned. No matches is a successful result with `papers: []`; a query failure
or timeout raises an error rather than returning a partial combined result.

**Landscape handoff:** use `paper_id` to load the paper's full structured
analysis, and retain `record_id`, `source_locations`, and query matches as
provenance. Returned records are only the retrieved subset, not the complete
paper analysis. This output does not include loaded `paper.json`, Markdown
pages, PaperCards, reranking assessments, landscape clusters, opportunities,
or hypotheses. Evidence loading and paper-context assembly remain separate
stages. Retrieval scores establish candidate order; downstream reasoning must
assess relevance, claims, coverage, and evidence. Preserve the actual queries
with a saved run because the planner's expansions may vary between runs.

### Service integration

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
