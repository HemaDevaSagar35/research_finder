# Implementation Status

Living document: what exists, what's in progress, what's next. The *why*
behind decisions lives in `offline_ingestion_design.md`; this page is the
*what*. Update this file whenever a component is added or materially changed.

Last updated: 2026-10-03 (Opportunity Miner implemented and smoke-tested; older offline status below has not been revalidated against the
running backfill or deployed services).

## Implemented

### Corpus acquisition (`scrapper/`)
| Component | What it does | Run |
|---|---|---|
| `browser_download_papers.py` | OpenReview venues behind Cloudflare, via your Chrome (ICML, MLSys, COLM, ...) | `uv run scrapper/browser_download_papers.py --venueid ICML.cc/2026/Conference` |
| `download_papers.py` | OpenReview venues via the plain API | same, for venues without bot protection |
| `virtual_download_papers.py` | ECVA-style virtual conference sites (ECCV 2026) | `uv run scrapper/virtual_download_papers.py` |
| `acm_download_papers.py` | dblp TOC listing + ACM DL open-access PDFs (SIGIR 2026) — browser-navigation downloads to beat Cloudflare's fetch blocking | `uv run scrapper/acm_download_papers.py` |
| `probe_venueid.py` | Diagnose a venue's real OpenReview venueids (or that it publishes none) | `uv run scrapper/probe_venueid.py ACM.org/SIGIR/2026/Conference` |

All scrapers stamp a hash-based `paper_id` into `papers/metadata.json`
(merged across venues) and name PDFs `papers/<venueid>/<title>.pdf`.
Corpus so far: ICML, ECCV, SIGIR downloaded (~10.8k metadata entries);
MLSys/COLM listed. `tools/upload_s3.py` bulk-uploads PDFs to S3.

### Extraction (`extraction/`) — per-paper, validated on 1 test paper
| Component | What it does |
|---|---|
| `pdf_to_markdown.py` | PDF → per-page markdown (LLM), page classification stops at references, per-page resume |
| `research_extract.py` | pages → `paper.json` (`PaperAnalysis` schema) with client-side schema repair |
| `extract_summary.py` | pages → detailed `summary.md` |

### Ingestion machinery (`ingestion/`) — written, not yet run at scale
| Component | What it does | Run |
|---|---|---|
| `worker.py` | ONE paper end-to-end: PDF (fetched on demand from `S3_PAPERS_URL` if not local, deleted after success) → pages → S3 → paper.json → summary.md → S3. Unit of work for backfill now and the upload service later | `uv run python -m ingestion.worker <paper_id>` |
| `backfill.py` | Batch driver: all of metadata.json, N workers, SQLite status manifest (`done`/`failed`/`no_pdf`), resume + `--retry-failed` | `uv run python -m ingestion.backfill --workers 2` |
| `s3store.py` | S3 artifact store helpers (`S3_ARTIFACTS_URL`), skip-if-exists uploads | library |

### Indexing (`indexing/`) — local path validated on 1 paper; OpenSearch path written, untested against a real domain
| Component | What it does | Run |
|---|---|---|
| `ids.py` | `paper_id` hashing + filename helpers | library |
| `flatten.py` | paper.json → typed records (`records.jsonl` + `papers.jsonl`); streams from S3 with `--s3` or local folders; sentence-aligned chunking at `CHUNK_CHARS` | `uv run python -m indexing.flatten --s3` |
| `embeddings.py` | Embedding providers: openai / gemini / local (fastembed, no key) | library |
| `build_index.py` | records → embeddings (record_id-keyed cache) + local FAISS/BM25/SQLite | `uv run python -m indexing.build_index` |
| `search.py` | Local hybrid search CLI (RRF) | `uv run python -m indexing.search "query"` |
| `opensearch_index.py` | AWS OpenSearch backend: index creation (embedding model pinned in mapping), bulk load from local artifacts, per-paper upsert, hybrid BM25+kNN search with client-side RRF | `uv run python -m indexing.opensearch_index --create --load-local` |

### Reasoning (`reasoning/`) — Cross-Paper Reasoner tested offline and smoke-tested live on three local papers
| Component | What it does | Run |
|---|---|---|
| `schemas.py` | Shared Landscape input (`landscape_builder_v1`; legacy `landscape_v1` supported), draft/review contracts, `CrossPaperReasoning` output; `validate_landscape()` referential checks | `uv run python -m reasoning.schemas --check` / `--landscape L.json` |
| `evidence.py` | `PaperStore` (local-first, lazy S3, hashes), `paper_card` projection, evidence `candidates` with JSON Pointer paths + inherited provenance, budgeted `bundle` | library |
| `budget.py` | attempt counter (reserve before request) and token estimates | library |
| `retrieval.py` | optional ranking hints over `indexing.search` (guards empty paper set, lazy import) | library |
| `cross_paper.py` | threads → draft → structural resolution → page-based support review → accepted findings / observations / tensions + typed diagnostics, coverage, usage | `uv run python -m reasoning.cross_paper --landscape L.json --out out.json [--chat fake]` |
| `fixtures.py`, `fake.py` | 4-paper synthetic corpus + landscape; cooperative fake model for tests / smoke runs | `uv run python -m reasoning.fixtures --out DIR` |
| `tests/reasoning/` | 53 offline tests pinning the failure-mode table in `docs/cross_paper_reasoner.md` | `uv run pytest tests/reasoning` |

Design, contracts, and deferrals: `docs/cross_paper_reasoner.md`. `llm_client` gained an additive
`ChatResult` / `chat_result()` so concurrent callers see their own
`finish_reason`.

### Opportunity mining (`opportunities/`) — section 6 implemented

`OpportunityMiner.run(landscape, reasoning)` proposes unresolved questions from
accepted findings, observations, and tensions, and reviews the original pages
before promotion. Exact landscape/reasoning lineage, extraction paths and hashes,
page hashes, limitation origins, and single-/multiple-paper support are retained.
Independent batches and reviews overlap under one concurrency and attempt budget.

[Contract, CLI, and validation](opportunity_miner.md): 126 tests plus 13 subtests
pass across the repository (29 tests cover this component). A bounded live run
proposed three questions from one reviewed finding and accepted all three after
page review, using five calls. One accepted scope retained a hardware-description
imprecision identified in review notes; automated acceptance is not a guarantee
of scientific correctness. Full-corpus novelty remains unassessed.

Broader Opportunity Miner validation is complete: [results](opportunity_validation.md).
135 tests plus 13 subtests pass; 124 live calls covered category controls and two
fresh corpus pipelines (21 proposed, 17 accepted, 4 rejected opportunities).
A factual-scope error was accepted in all three repeat probes, and one
`multiple_papers` label includes a paper cited only as contrary context. These
quality findings are addressed by [review and paper-role fixes](opportunity_review_fixes.md):
`opportunities_v2` blocks flagged factual corrections and counts only reviewed
supporting papers. Validation now totals 149 tests plus 13 subtests, with 32
focused live calls. Error detection still depends on model judgment.

Opportunity proposals now receive all reviewed sources together by default
(`batch_size=0`); positive batch sizes explicitly opt into partitioning. Active
generation input/output allowances are 500000 tokens. The shared DeepSeek client
caps actual output requests at the configured API maximum of 393216. Reasoner
bundle allowance is 2000000 characters with a complete-prompt token guard.
157 tests plus 13 subtests pass, including complete-context delivery and token
configuration/output-cap regressions. These changes were checked offline;
new live generation-quality checks remain to be run.

## Architecture decisions in force
(rationale in `offline_ingestion_design.md`)
- Extraction and indexing are **separate stages**; S3 is the contract between
  them (indexing reads `paper.json` from S3, never local disk, in production).
- Artifacts upload to S3 **as soon as produced** (pages before the
  single-call extraction stages) — local disk is a working cache.
- Only flattened `paper.json` records are embedded/indexed; markdown pages
  are the evidence store; `summary.md` is not indexed (revisit if needed).
- OpenSearch replaces FAISS+BM25+SQLite in deployment; the local triple stays
  for dev. Managed domain, not Serverless (client `_id` needed).
- No SQS for the backfill; queue only arrives with the upload service.

## Offline checklist (recorded 2026-09-20; historical)

As of 2026-09-27, the user reports offline ingestion is nearly complete.
The checklist below is retained as historical context, not a current statement
that backfill has yet to start. Current corpus/index details are documented in
`index_contract.md`.

1. **Run the extraction backfill** on the corpus (EC2 or laptop, tmux):
   small `--limit` first, inspect artifacts, then let it run. This is the
   long LLM crunch — everything else is hours, this is days/weeks.
2. Inspect early `paper.json` output → settle **embedding provider** and any
   flatten/chunking tweaks (decision currently open: local bge vs API).
3. Provision the **OpenSearch domain**; `--create` the indices; bulk load
   via `flatten --s3` → `build_index` (embeddings) → `--load-local`.
4. Sanity-check retrieval quality with `opensearch_index --search`.

## Online stage — proposed build order

**Deployment requirement:** the full query-to-hypothesis pipeline will run on
a server as a service, exposed through an API. Build the reasoning modules for
that environment from the start, using OpenSearch and S3 in production, with
isolated request state. Hosting, API framework, and sync/async execution protocol
remain undecided. Reuse one retriever per service process/event loop, with a
bounded worker pool and request-local results. Drain workers before closing
shared backend clients; configure backend network timeouts and request admission
control when wiring the API. This generation service is distinct from the deferred
user-upload/ingestion service below.

Query planning, multi-query retrieval, landscape construction, cross-paper
reasoning, and opportunity mining are implemented as modules. Reasoning has a
local/S3 artifact reader; unified service wiring, reranking, and downstream
generation remain incomplete. Continue validating the
**query-to-evidence foundation**, described in
[architecture §2](research_path_generator_architecture_refined.md#2-query-to-evidence-foundation)
and [component contracts §2](research_path_generator_components_refined.md#2-initial-retriever).

1. Query planning is implemented in `research/query_planner.py`: async LLM
   expansion into `QueryPlan(queries=[...])`, original-query preservation,
   deduplication, validation, and a CLI. Six offline tests cover validation,
   normalization, request isolation, and client cleanup; live model quality
   has not yet been evaluated. Retrieved paper/record contracts are implemented in `research/retrieval.py`;
   resolved evidence and paper-context contracts remain to be defined.
2. Add a common local/S3 reader for `paper.json` and cited Markdown pages;
   derive compact PaperCards by field projection.
3. Multi-query paper-level retrieval is implemented in `research/retrieval.py`:
   local/OpenSearch adapters, decaying record-to-paper rollup, equal-weight
   paper RRF, query provenance, evidence pointers, shared type/year filters,
   bounded async execution, deadlines, and CLI. Local searches share one lazily
   loaded FAISS/BM25 index per backend with synchronized initialization, concurrent
   read-only searches, and per-search SQLite connections. BM25 arrays use memory
   mapping. Drain the retriever before closing/replacing its local backend.
   CLI progress goes to stderr and JSON results to stdout. Nineteen offline tests
   pass, including real FAISS/BM25/SQLite fixtures for concurrent load-once behavior,
   ranking equivalence, filter isolation, failed-load retry, and index cleanup.
   Live OpenSearch/model quality remains unverified. Add reranking that
   preserves relevance, explicit constraints, and coverage across approaches.
4. Validate that a query returns an inspectable paper selection with matching
   records and resolvable evidence before adding hypothesis generation.
5. Landscape construction, cross-paper reasoning, and opportunity mining are
   implemented. Integrate these into the service and build
   direction/hypothesis/experiment generation on their contracts.
6. Add full-corpus novelty retrieval, candidate-vs-prior-work comparison,
   refinement, critique, ranking, and the final portfolio.

Integration gaps observed in the current code:

- Local search now returns `source_locations` and closes SQLite connections
  on failure. Retrieval adapters normalize both backends to the same paper
  contract, with common type and exact-year filtering. Broader backend-specific
  filters are not exposed by this contract.
- `reasoning.evidence.PaperStore` provides local/S3 artifact access. Shared
  online service wiring and request-scoped usage accounting remain needed.

These are planned logical modules, not separate services or agents. Novelty
assessments remain relative to the available 2026 corpus.

## Pinned / deferred
- **User-upload service** (API + SQS + same worker): pinned until backfill
  works. Design note: user PDFs have no scraper metadata, so the title must
  be extracted (first page, cheap LLM call) before `paper_id` can be minted.
- Indexing `summary.md` sections as extra records — only if retrieval
  quality shows narrative-context gaps.
- Online stage (research-direction generation) — planned above; detailed
  responsibilities remain in the architecture and component design docs.
  The Cross-Paper Reasoner (component 8) has completed a small real-provider
  local-corpus run. See [live results](live_landscape_reasoning_smoke.md) for
  truncation, task-selection bias, and manual qualification of accepted output.
  Deferred inside the reasoner: narrowing redraft after review rejection,
  per-thread cache, heading-based page inference for `page=None` locations.

### Landscape → reasoner integration

The builder contract is shared directly with the reasoner. Deterministic item
IDs and per-paper/per-record relationship evidence preserve downstream links.
The builder algorithm and top-level collections are unchanged. An offline
integration test covers builder orchestration, JSON round-trip, reasoning,
review-source hashes, references, and paper-selection bounds. Real-corpus model
quality is not established by this test.

Live integration evidence: [three-paper smoke test](live_landscape_reasoning_smoke.md).

Limitation origin is now preserved from extraction through aggregation, drafts,
page review, and output evidence. [Attribution validation](limitation_attribution_validation.md)
records 97 passing tests plus 13 subtests and a real-provider rerun of the
three-paper inferred-limitation cluster.

### Opportunity Miner full-context validation

[Boundary validation](opportunity_boundary_validation.md) passed 12 synthetic
reviews and three real miner runs using saved reviewed upstream artifacts.
Each real run supplied all its reviewed sources in one proposal context, with
concurrent original-page reviews; 12 of 15 real candidates were accepted and
three withheld. All 18 accepted outputs across controls and real runs passed
evidence and paper-role audits (63 evidence references, 70 page references).
34 live calls completed without truncations or provider errors. The offline
suite passes 159 tests plus 13 subtests. These checks validate evidence
boundaries and the v2 handoff, not scientific value or literature-wide novelty.

### 2026-10-03 implementation history

The module-level approach and before/after behavior for the above full-context,
token-allowance and validation changes are recorded in
[Implementation updates](implementation_updates.md). Existing module documents
retain their previous descriptions and append dated sections identifying which
statements are superseded. Validation counts above reflect successive runs;
the latest completed suite is 159 tests plus 13 subtests.
