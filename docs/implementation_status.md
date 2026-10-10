# Implementation Status

Living document: what exists, what's in progress, what's next. The *why*
behind decisions lives in `offline_ingestion_design.md`; this page is the
*what*. Update this file whenever a component is added or materially changed.

## 2026-10-10 — Paper bibliography added

Final JSON/Markdown now includes titles, authors, year, venue, public and PDF
links, plus source provenance and explicit missing/conflicting metadata. Catalog
records are joined by exact paper ID; extraction metadata provides a fallback.
Local query runs discover the index/download metadata automatically. Saved v1
portfolio artifacts remain readable. An offline enrichment of the real v13 run
resolved all 53 paper titles and public/PDF links; its scientific blocked status
was preserved. See [reference documentation](final_portfolio.md).

## 2026-10-10 — Query-to-portfolio runner implemented

`python -m research "QUERY" --out RUN_DIR` now connects all existing stages
through final portfolio selection. It supports local/OpenSearch retrieval,
local-first/S3 evidence, typed per-stage checkpoints, bounded refinement, and
configuration-bound resume. The CLI emits final JSON/Markdown and an explicit
ready/partial/blocked/empty/failed summary. API hosting remains separate work.

This supersedes the earlier statement that no initial-query runner exists. It
does not imply a successful new real-provider scientific portfolio: validation
uses offline integration tests, and existing scientific blockers remain intact.
See [runner documentation](query_pipeline.md).

## 2026-10-09 — Final outputs implemented

Architecture sections 16–17 now live in `portfolio/`: typed final candidate
reports, deterministic ranking over reviewed signals, 3–5 direction selection,
reserve/discard/pending accounting, and JSON/Markdown CLI exports. Only candidates
with the final `ranking` handoff can be selected. Existing source artifacts and
novelty scope are preserved and revalidated on load.

The latest real candidate remains blocked on H4/E4 correctness; an offline replay
correctly returns zero selected and one pending candidate. This implementation
does not resolve that scientific issue or deploy the online service. Details and
validation: [Final portfolio](final_portfolio.md). The status notes below retain
their historical dates; later module documentation supersedes them.

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

Broader Opportunity Miner validation is complete: [results](opportunity_miner.md#broader-validation-history).
135 tests plus 13 subtests pass; 124 live calls covered category controls and two
fresh corpus pipelines (21 proposed, 17 accepted, 4 rejected opportunities).
A factual-scope error was accepted in all three repeat probes, and one
`multiple_papers` label includes a paper cited only as contrary context. These
quality findings are addressed by [review and paper-role fixes](opportunity_miner.md#review-fixes-history):
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
  local-corpus run. See [live results](cross_paper_reasoner.md#integration-live-validation) for
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

Live integration evidence: [three-paper smoke test](cross_paper_reasoner.md#integration-live-validation).

Limitation origin is now preserved from extraction through aggregation, drafts,
page review, and output evidence. [Attribution validation](cross_paper_reasoner.md#attribution-validation)
records 97 passing tests plus 13 subtests and a real-provider rerun of the
three-paper inferred-limitation cluster.

### Opportunity Miner full-context validation

[Boundary validation](opportunity_miner.md#boundary-validation) passed 12 synthetic
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
[Implementation updates](README.md#module-documentation). Existing module documents
retain their previous descriptions and append dated sections identifying which
statements are superseded. Validation counts above reflect successive runs;
the latest completed suite is 159 tests plus 13 subtests.


### 2026-10-03 documentation consolidation — supersedes the separate-log layout above

Implementation history now lives in one file per module: [Landscape Builder](landscape_builder.md#implementation-history),
[Cross-Paper Reasoner](cross_paper_reasoner.md#implementation-history),
[Opportunity Miner](opportunity_miner.md#implementation-history), and
[shared LLM client](llm_client.md). This status file is only the project overview.
The landscape and reasoner were existing modules that we modified; the miner
was a new module introduced at `847b910`. Separate integration/validation/fix
reports were merged into their module documents, preserving historical results
and adding explicit before/after changes. No pipeline behavior changed in this
consolidation.


### 2026-10-03 — Direction Generator initial implementation

The new `directions/` module implements architecture sections 7–8 and component
contracts 10–12. This supersedes the earlier build-order entry listing direction,
hypothesis and experiment generation as unimplemented. It consumes the exact
landscape/reasoning/accepted-opportunity artifacts and original papers, generates
coupled directions/hypotheses/experiments concurrently across opportunities, and
validates evidence lineage and experiment links. Novelty, refinement, scientific
critique, ranking and online service wiring remain downstream work.
[Direction Generator](direction_generator.md) contains the single module document,
implementation history, usage and validation results.


### 2026-10-04 — Direction Generator validation completed

The new module passes 40 focused tests; the complete suite passes 199 tests plus
13 subtests. An initial 11/12 live result exposed ambiguity between contextual
upstream IDs and opportunity-selected citations. The prompt/context projection
was corrected without relaxing validation. The fresh rerun produced 12 directions,
48 hypotheses and 47 experiments from all 12 accepted opportunities across the
three saved real-paper sets, using 13 calls with peak concurrency four. All 51
evidence and 58 page references passed a saved-output artifact audit. Full
history, preserved initial failure, reproduction and boundaries are in the
[single Direction Generator document](direction_generator.md).


### 2026-10-04 — Direction Generator preservation checks and open draft issues

Follow-up validation adds eight offline checks (complete suite: 207 tests plus
13 subtests) and four synthetic live controls, each repeated twice. A baseline
exposed overstated measurement history and an invalid experiment/hypothesis link;
the existing generator's instructions were tightened. A fresh eight-call run
preserves tension sides, conditions, selected gaps and untested effects, but two
experiment-draft inconsistencies remain. This supersedes any reading of the
previous validation entry as a blanket scientific or experimental-quality pass.
Exact changes, baseline/fresh-run results and remaining failures are appended to
the single [Direction Generator document](direction_generator.md).


### 2026-10-04 — Direction Generator experiment-consistency follow-up

The existing generator now defines consistent experimental arms and metric
applicability, distinguishes seed blocks/repetitions, and requires each linked
experiment to include the hypothesis's comparison. Two fresh live runs used
17 calls. In the final eight-output sample, the two original entropy/cache-metric
mistakes did not recur and all outputs passed structural/source-preservation
checks, but the expanded manual consistency rubric still failed in four outputs.
The final offline suite remains 207 tests plus 13 subtests passed. This supersedes
the prior status of the two targeted mistakes for the tested sample, while broader
experimental coherence remains open. Full details and preserved failure reports
are in [Direction Generator](direction_generator.md).


### 2026-10-04 — Direction Generator scope correction (`directions_v2`)

The generator now produces tangible research directions, testable hypotheses and
concise suggested tests, matching the user's clarified scope and architecture
sections 7–8/16. Mandatory experimental setup, controls, resource estimates and
dependency graphs have been removed; the earlier protocol-validation expansion
is superseded. The first useful test, hypothesis links, evidence lineage and
unreviewed-candidate status remain. The new contract explicitly rejects v1 output.

Validation: 211 tests plus 13 subtests passed; eight synthetic live outputs passed
contract/source/scope checks, with remaining hypothesis/test refinement notes
recorded separately. All 12 real-paper input handoffs passed an offline audit.
Supplementary real-paper live calls were blocked by automatic approval review
for lack of explicit payload-export authorization and did not execute.
The single [Direction Generator document](direction_generator.md) records the
exact module changes, compatibility, scope correction and full validation history.


### 2026-10-04 — Direction Generator v2 real-paper live check completed

This supersedes the pending real-paper export status in the previous entry: after
explicit user authorization, all 12 saved opportunities produced v2 directions
through DeepSeek (43 hypotheses, 41 suggested tests, 12 calls, peak concurrency four).
All persisted-contract and upstream handoff checks passed without repairs or errors.
Manual review of all 12 outputs confirmed the requested tangible-direction and
suggested-test scope, while recording unresolved hypothesis/test alignment,
falsification, provenance and measurement-interpretation issues. These are draft
refinement notes, not a scientific-quality pass. Original outputs remain preserved.

No production implementation changed in this follow-up. The last full offline suite
remains 211 tests plus 13 subtests passed. Exact counts, module scope, validation
artifacts and content-review limits are appended to the single
[Direction Generator document](direction_generator.md). Novelty assessment remains
the next unimplemented architecture stage, followed by refinement and critique.


### 2026-10-04 — Generator correction tested; basic prose errors remain open

`directions/generator.py` now preserves the cross-paper condition `hypothesis`
flag instead of flattening conditions to text. The final prompt version is
`direction_v2_explicit_comparisons`; attribution, constructed comparisons for
unknown baselines, outcome referents and hypothesis success criteria are clarified.
Field descriptions in `directions/schemas.py` agree with these instructions;
the v2 output shape remains unchanged. New regression and live-harness preflight
checks bring the full offline suite to 217 tests plus 13 subtests passed.

Two bounded live runs made 16 API attempts and generated 15 outputs. All persisted
contract/reference checks passed. **Content correctness is not closed:** manual
review of the final five candidates still found basic errors in two (a source
misstatement and contradictory outcome classification, including an at-least-two
prediction called inconclusive when only one model improves). These are not all
later-stage refinement issues. No novelty implementation was started, and the
previous recommendation to advance is superseded. The single
[Direction Generator document](direction_generator.md) preserves the complete
diagnosis, changed modules, reproduction commands, both runs and precise remaining
failures. Original generated artifacts remain unchanged.


### 2026-10-04 — Independent Direction Generator correctness judge implemented

The existing `directions/` module now requires fresh-context correctness review,
allows one substantive revision followed by a fresh review, and withholds drafts
on unresolved issues, abstention, invalid review or unavailable review. New
`directions/judge.py` handles the narrow review rubric and source-quote checks;
`directions/generator.py` orchestrates it and `directions/schemas.py` exports the
`directions_v3` contract with exact passing-draft audit records. Independent
opportunities remain asynchronous under shared budgets. Reviewer model selection
is configurable; default uses the same provider/model in a separate context.
Detailed protocols, scientific merit and novelty remain outside this judge.

Validation: **239 tests plus 13 subtests passed**. Eighteen authorized DeepSeek calls
tested sound/error controls and saved real drafts, including two full
review/revision/review flows. The initial judge missed residual errors after
revision; a focused reviewer update now catches the MOMENTKV equality
contradiction. Final follow-up met five of six expected decisions: the judge still
passed RAG's unsupported assertion about an original prompt property. This known
attribution miss is documented, not described as a clean correctness pass. The
runtime gate and audit implementation are complete; semantic review remains
fallible. Novelty was not implemented or certified.

The single [Direction Generator document](direction_generator.md) records changed
files, current behavior, compatibility, budgets, reproduction commands and both
live runs, preserving earlier implementation history.


### 2026-10-04 — Novelty signatures and full-corpus retrieval

New `novelty/` module implements architecture sections 9–11: load and hash-check
source `paper.json` and original reviewed pages, decompose every reviewed direction
and hypothesis into provenance-bearing signatures, independently review fidelity,
search the full configured index and rerank retrieved PaperCards. A bounded
signature revision is allowed; unreviewed signatures never search. Result contract
is `novelty_search_v1`, with exact candidate/review/query lineage and explicit
partial/failed retrieval. Novelty comparison and verdicts remain unimplemented.

Existing direction, landscape, reasoner, miner and search modules are reused without
edits. Default allowances are 500,000 input/output tokens with bounded asynchronous
calls. Documentation is consolidated in the single
[Novelty Search module document](novelty_search.md), including the initial live
contract failure, correction and validation results.


Novelty-stage validation completed with **276 tests plus 13 subtests passing**
(37 new module tests). Local index check: 7,225 papers, 983,126 records. Twelve
explicitly authorized DeepSeek attempts validated signature correction/review,
five full-corpus hybrid searches and representative reranking. A JSON-mode prompt
bug affecting four reranks was fixed and verified on one previously failing target;
three hypothesis reranks were not repeated within the approved call bound. An
unestablished causal premise remains inside a proposed-mechanism facet rather than
being fully split into an unknown; it is not a source-fact claim. Full live coverage,
failed runs, fixes and limits are recorded in [Novelty Search](novelty_search.md).
No candidate-versus-prior-work novelty verdicts have been implemented yet.


### 2026-10-04 — Novelty validation follow-up completed

The prior three pending reranks now pass. `novelty/prompts.py` distinguishes
unsupported implementation premises from conditional research hypotheses;
paired live controls reject the historical error while accepting conditional and
source-supported versions. A fresh RAG signature explicitly marks original prompt
contents unknown and makes the explanation conditional, closing that specific
novelty-signature regression in the tested case. Original upstream drafts remain
unchanged.

Fresh full-depth RAG and MOMENTKV runs completed all ten targets, each with a
20-paper shortlist; all five predeclared related-paper controls appeared in at
least one pool and shortlist. Coverage is reported per target, not as a guarantee
of exhaustive recall. Follow-up: 27 DeepSeek calls; full suite: **276 tests plus
13 subtests passed**. Reproducible tools, source-backed controls, failed initial
fixtures, full results and call ledger are documented in the single
[Novelty Search module document](novelty_search.md). Candidate-versus-prior-work
comparison and novelty verdicts are still the next unimplemented stage.

## 2026-10-04 — Section-12 original-evidence comparison implemented

`novelty/` now adds `comparison.py`, `comparison_schemas.py`,
`comparison_prompts.py` and the `novelty.compare` CLI. It compares every shortlisted
pair across the architecture's eight dimensions using original available page
markdown, with paper-level concurrent batching, fresh independent review, one
substantive revision, and exact reviewed-output binding. The existing
`novelty/pipeline.py` call helper also routes comparison reviews to the configured
reviewer. Earlier notes that comparison was unimplemented are superseded by this
entry. Pairwise overlap labels do not certify literature-wide novelty.

The module approach, precise edits, CLI and scope are appended to the single
[Novelty Search document](novelty_search.md). Added 42 comparison tests; the full
suite passes **318 tests plus 13 subtests**. Offline preflight found matching artifact
hashes and available original pages for all 54 shortlisted papers in each of the
two saved topics. Live comparison validation is pending specific payload approval:
automatic approval review blocked the new prior-paper JSON/page transfers to
DeepSeek. No section-12 live results are claimed. Aggregate novelty interpretation,
refinement and research critique remain downstream work after validation.

## 2026-10-04 — Section-12 live tests completed; acceptance failed

Committed implementation as `4c73e34` before testing. Completed **63 DeepSeek calls**
on seven selected papers (30 target–paper pairs), including targeted retests and
semantic judge controls. Latest outcomes: **one paper passed the automated gate,
five remain unresolved, and one failed exact-quote validation**. Earlier passing
results that later checks contradicted are not counted as accepted. Manual inspection
also found a scope-wording caveat in the gate-passing ReST-KV result; no clean overall
acceptance or novelty conclusion is claimed.

Follow-up edits improve exact quote/pointer diagnostics, candidate-uncertainty and
claim-level citation prompts, and add one audited review-format repair. Added a live
control tool. **321 tests plus 13 subtests pass.** The implementation, per-file changes,
all live failures, final results, 63-call ledger and next required evidence-construction
work are appended to [novelty_search.md](novelty_search.md). These live-test follow-up
fixes and documentation updates are uncommitted. All calls have finished; no approval
block remains. Section-12 evidence fidelity must improve before progressing to novelty
aggregation/refinement.

## 2026-10-04 — Section-12 reference-based redesign implemented and tested

This supersedes the proposed evidence-construction work in the preceding entry.
`novelty/comparison_evidence.py` now supplies stable source-passage IDs, exact candidate
views and resolved evidence. `comparison_schemas.py`, `comparison.py` and
`comparison_prompts.py` use preserved reviewed targets and shared prior claims instead
of copied candidate statements/quotations. `comparison_legacy.py` retains the v1 reader.
Full approach, per-module changes, compatibility and test findings are appended to the
single [novelty_search.md](novelty_search.md) document; older implementation history is
preserved.

**331 tests plus 13 subtests pass.** Offline replay preserved all source characters
and candidate/unknown fields across seven historical payloads. Eleven historical
artifacts remain readable. Completed **37 new DeepSeek calls**: seven-paper rerun
plus four semantic judge controls. Automated outcomes improved to **19/30 pairs
passing and 11 withheld**; 170 unselected pairs remain skipped. Both false-tested
injections were caught, but both baseline re-reviews returned revise, and source
inspection found both real missed defects and an inaccurate reviewer premise.

This is **not clean semantic acceptance**. Remaining failures concern secondary
clauses missing supporting passages, source qualifiers lost in prior claims, and
unqualified absence wording. Stable references and unchanged candidate text now work;
scientific entailment/reviewer consistency remain the limiting factors. No aggregate
novelty conclusion is produced and sections 13–15 should not treat these examples as
validated. All calls finished; no approval blocker remains. Changes are uncommitted.

## 2026-10-04 — Section-12 evidence-first comparison implemented and validated

This extends the reference-based redesign above. New `comparison_records.py` and
`comparison_workflow.py` add per-claim evidence audits, per-target omission review,
explicit empirical/theoretical/discussion/inference relationships, bounded evidence
reopening and dependency-based partial publication. Existing `comparison.py`,
`comparison_schemas.py`, `comparison_prompts.py` and `pipeline.py` integrate these
stages and preserve separate review routing. Final artifacts use v4; dedicated v2/v3
readers preserve historical results. Final proposal anchoring prevents background
problem evidence alone from counting as investigation of the proposed intervention.

**355 tests plus 13 subtests passed.** Completed **85 additional DeepSeek calls**,
including failed initial cases, corrections and eleven final controls that all
matched expectations. Latest artifacts contain automated accepted comparisons for
all thirty selected pairs across seven papers; 170 unselected pairs remain skipped.
The two source papers were rerun with final v4 anchoring; other selected results
remain v3. Source RAG's false direct-coverage result is corrected to related evidence;
MOMENTKV's mechanism discussion is separated from testing the proposed diagnostic.

This does not certify every sentence: source inspection still found omitted model
qualification/caption citations and historical unqualified absence prose. Exact
examples, the ambiguous earlier control, version boundaries and the complete run
ledger are appended to the single [Novelty Search module document](novelty_search.md).
No literature-wide novelty verdict is produced. Sections 13–14 remain downstream;
no unassessed or unresolved result should be interpreted as novelty. All calls have
finished. These implementation and documentation changes remain uncommitted.

## 2026-10-04 — Result-scope and citation fixes tested; live acceptance remains mixed

The section-12 follow-up adds explicit assertion/condition checks to existing claim
reviews, deterministic table-caption/setup attachment, persisted context validation,
and correct routing of citation gaps through bounded evidence repair. It also fixes
the eligible-target contract sent to interpretation reviewers and partial evidence-view
rendering. The implementation remains inside `novelty/`; per-module changes and all
results are appended to [novelty_search.md](novelty_search.md).

**369 tests plus 13 subtests pass.** Completed **92 additional DeepSeek API calls**,
with exact saved-response replays accounted separately. The original MOMENTKV model
qualification/Table 5 references and CriticalKV table-caption issue are corrected in
inspected outputs. Latest MOMENTKV handoff: three accepted pairs, two withheld for a
remaining formula-reference gap. CriticalKV has an earlier complete corrected run,
but the latest replay missed the uncited alpha-setting clause again and is explicitly
flagged unsuitable for downstream use in its manual findings. Final standalone review
controls matched 6/9 expectations; failures and the subsequently corrected caption-layout
bug remain documented. This is not clean semantic acceptance or a novelty verdict.
All calls have finished; changes remain uncommitted.

## 2026-10-04 — Sections 13–14 implemented: joint novelty assessment and refinement

Added `novelty/assessment.py`, `assessment_schemas.py`,
`assessment_evidence.py`, `assessment_prompts.py`, and the `novelty.assess`
CLI. Existing `novelty/pipeline.py` now routes independent assessment reviews.
Each direction is assessed with all its hypotheses and accepted comparisons in
one context; independent directions run concurrently. Exact artifact lineage,
explicit manually invalidated evidence, and all missing/skipped/failed comparison
coverage are retained. Refinement preserves original proposals and requires new
novelty checks for scientific edits.

**447 tests plus 13 subtests passed**, including 78 assessment tests.
**36 DeepSeek calls completed.** The final six synthetic boundary controls matched
expectations. The final inspected MOMENTKV synthesis reports partial overlap for
H1/H3/H4 and unresolved direction/H2; it defers remaining novelty because shortlist
coverage is incomplete. The manually rejected CriticalKV replay is blocked with
zero calls. Earlier live failures, reviewer misses, their fixes, and per-module
changes are preserved in the existing [Novelty Search document](novelty_search.md).

Sections 13–14 are implemented and tested within that scope. Completing real
shortlist coverage and the earlier section-12 evidence repairs remains separate
from implementing section 15's scientific critic. No literature-wide novelty or
scientific usefulness guarantee is made. Refinement edits are proposed, not
silently applied. Changes are not committed.

## 2026-10-04 — Full MOMENTKV shortlist and sections 13–14 executed

The complete saved shortlist was attempted: **100 target–paper pairs across 54
papers, zero skipped**. Fresh comparisons and documented recoveries produce 69
accepted pairs and 31 withheld pairs. All 100 coverage entries are carried into
joint synthesis; no selected-paper diagnostic substitutes for this full run.
Both MOMENTKV and CriticalKV have all five comparisons accepted in the new
artifacts. Remaining scope/citation/format failures are explicitly recorded.

The local novelty input allowance is now **1M**. A provider context rejection
exposed a difference from the local token estimate. `novelty/pipeline.py` now
uses compact JSON and sizes actual message text; `assessment_evidence.py` and
`assessment.py` send every source passage's full text and ID while preserving
repeated provenance metadata in the immutable input artifacts. The full
assessment uses a 65,536-token output reservation. No findings or source text are
removed. Assessment prompts v5 additionally prevent unsupported transfers of
conditions between separately scoped claims.

The final independently reviewed assessment publishes **PARTIAL_OVERLAP for the
direction and all four hypotheses**. It retains all four, with refinement
**defer**: the high-spread questions remain distinct in accepted evidence, but
31 withheld comparisons leave remaining novelty unresolved. This is five
section-14 outcomes, not zero outputs or confirmed novelty. The saved handoff
routes to resolving comparison coverage; section 15 has not run.

**174 novelty tests pass**, including 80 assessment tests. A fresh live regression
correctly rejects the earlier unsupported sigma-to-ablation linkage; the corrected
full synthesis passes fresh independent review and focused source inspection.
The full execution used 380 API requests (379 responses, one context rejection)
and six local extraction replays; all calls have finished. Module changes,
rejected attempts, recovery provenance and limitations are appended to the single
[Novelty Search module document](novelty_search.md). Final results are under
`/home/hema/research_runs/novelty_kv_consolidated_v1/` and
`/home/hema/research_runs/novelty_kv_full_assessment_v3/`. Changes are uncommitted.

## 2026-10-04 — Targeted repair resolves all 31 withheld comparisons

Section 12 now uses sparse, source-checked repairs in the new
`novelty/comparison_repair.py`, integrated into existing comparison workflow,
comparator, prompts and schemas. Known missing citations are attached by code and
re-audited; semantic edits preserve unaffected claims/targets. Pair-rationale
errors no longer automatically regenerate evidence. Bounded follow-up patches
handle newly discovered defects, and repeated records stop further retries.
Comparison v6 preserves old v5 reading while recording expanded audited history.
Assessment transport uses reversible short source labels to retain the entire
scientific packet within the provider context; original references are restored
before validation and persistence.

The complete saved MOMENTKV shortlist is now **100/100 accepted comparisons across
54 complete papers, zero skipped or withheld**. The follow-up used **91 API calls**
and 47 separately counted local checkpoint replays. **223 novelty tests pass**;
all calls have finished. Final consolidation is
`/home/hema/research_runs/novelty_kv_consolidated_v4/result.json`.

The final sections-13/14 assessment retains all four original hypotheses with
**PARTIAL_OVERLAP** and complete recorded coverage. Their novelty status is now
`partial_overlap`, not `unresolved`; no edits/removals are applied. The saved handoff
routes to **section 15, Research Critic**, which has not run. Final assessment:
`/home/hema/research_runs/novelty_kv_complete_assessment_v5/result.json`.

A complete-context reviewer again missed the sigma-to-ablation wording defect in
an earlier synthesis; that attempt is manually rejected and preserved. The final
source-based correction passed fresh independent review and focused inspection.
Model review remains fallible; complete comparison coverage is not a global
novelty or usefulness guarantee. Exact module changes, recovery history, tests,
artifacts and limitations are appended to the single
[Novelty Search module document](novelty_search.md). Changes remain uncommitted.

## 2026-10-04 — Section 15 Research Critic implementation

New `critic/` evaluates every direction/hypothesis against the eight section-15
scientific questions, with independent review, explicit revisions and upstream
source-reassessment requests. Original opportunity artifacts/pages supplement the
accepted novelty evidence. Independent candidate calls run concurrently; joint
reviewed MERGE proposals preserve constituent hypotheses and require fresh checks
of changed scientific scope. No proposals are silently changed and no processing
failure becomes a scientific rejection. Final ranking is still downstream.

Module contracts, all added/changed files, approach and validation history are in
[Research Critic](research_critic.md), the single module document. Initial critic
and novelty regression checks pass (257 tests). Complete MOMENTKV input preparation
passes; the initial external live launch is blocked by automatic approval review
pending the specific payload authorization. This entry does not claim a live pass.

Section-15 follow-up checks now total **266 passed**, including full two-candidate
merge/review/handoff tests and concurrent request-local accounting. Saved corrections
must preserve unaffected judgments; surviving test links are retained. The full live
MOMENTKV test remains pending the exact payload authorization, not a completed or
failed model run. See the Research Critic module document for the distinction.

## 2026-10-05 — Research Critic committed and full live retry completed

Committed the initial implementation as `f6c9074`, then ran the explicitly
reauthorized full MOMENTKV payload. The first automated pass missed a genuine
H2/E2 equality/null contradiction. Added mandatory per-test-link consistency
checks, located internal-correctness requests, precise field inventories, linked-test
revision routing and narrowly scoped review-pointer normalization. Existing novelty
inputs and hypotheses remain unchanged.

The final full-context independent review detects the contradiction and correctly
routes to `direction_correctness`; **no scientific pass, rejection or ready-for-ranking
candidate is published**. This is an actionable proposal correction, not unresolved
novelty coverage. Final artifact:
`/home/hema/research_runs/research_critic_kv_live_v6/result.json`.
Source coverage is complete for this saved run (100 comparisons, 7,782 passages,
direction plus four hypotheses). Final tests: 54 critic and 223 novelty checks pass.
The module document records all API attempts, provider context/output failures,
checkpoint replays, the manually rejected pass, exact changed modules and limitations.

### 2026-10-05 — Critic correction/refinement loop

Added `critic/refinement_loop.py`, `directions/revision.py`,
`directions/revision_schemas.py`, `directions/test_links.py`, and
`novelty/revalidation.py`. Existing generator correctness reviews now explicitly
check every hypothesis/test link. Scoped revisions receive fresh original-page
review, versioned novelty applicability checks or fresh search/comparison, fresh
novelty synthesis, and a new critic. At most two full cycles run; unresolved
correctness, scientific refinement, evidence reassessment and merge requests stay
explicit. `tools/validate_refinement_loop.py` records full live calls/checkpoints.

Module details and actual live results: [Direction Generator](direction_generator.md),
[Novelty Search](novelty_search.md), and [Research Critic](research_critic.md).
The earlier implementation entries remain as history. Final portfolio ranking
and merge construction are still separate work.

Final loop validation: **545 tests plus 13 subtests pass**, including 29 new loop
checks. Twenty-four live calls across the complete attempts and final ownership
audit corrected the known H2/E3 proposal defects and verified withheld-result
behavior. The saved candidate still needs a scoped section-12 MOMENTKV/H3
interpretation repair and synthesis qualifier corrections before a new scientific
critic verdict. The implementation is complete; that candidate is not approved
for ranking. See the critic module's latest validation entries for the exact
artifacts and remaining reviewer-consistency limitation.


## 2026-10-05 — Scoped reassessment and complete comparison recovery

The preceding pending H3 interpretation and synthesis work now have implemented
recovery paths. New `novelty/reassessment.py` provides source-preserving scoped
interpretation reopening and incomplete-paper recovery, with validated parent,
fresh-attempt and merged artifacts. Existing `novelty/comparison.py` accepts a
reviewed evidence seed and sparse comparison corrections; existing
`novelty/assessment.py` supports exact-input synthesis reopening and source-scope
guidance, always followed by fresh independent review.
`novelty/comparison_records.py` accepts `outcome` as a scope-aspect label without
relaxing support/citation validation. New `tools/validate_reassessment.py` runs
recovery, full assessment, critique and authorized refinement with checkpoints.

The live critic's H1 intervention correction required fresh scientific novelty
checks. That complete new shortlist contains **100 target/paper pairs across 50
papers**. Its initial 18 withheld pairs were recovered through saved evidence,
located source corrections and fresh reviews. `momentkv_reassessment_v7` now has
**100/100 accepted pairs and 50/50 complete papers**, with the 49 completed papers
from v6 preserved exactly. Full sections-13/14 synthesis passed independent review:
the direction and all four hypotheses are `PARTIAL_OVERLAP`; all four hypotheses
are retained and all five targets have complete recorded coverage.

This supersedes the earlier comparison/synthesis blockers for this saved case.
It does not establish literature-wide novelty, scientific truth or universal
semantic-review reliability. The full Research Critic result follows in the
module's final live-validation entry. Implementation, attempts and exact artifacts
are documented in [Novelty Search](novelty_search.md) and
[Research Critic](research_critic.md); no separate per-attempt module docs are added.
Regression result: **565 tests plus 13 subtests passed**, one existing NumPy warning.


## 2026-10-06 — Full revised-candidate validation completed; H4 remains unresolved

This supersedes the earlier 100-pair/50-paper entry for the previous candidate.
After the second scientific correction, the latest candidate's full shortlist has
**100 accepted comparisons across all 53 papers**, with no skipped or withheld
comparisons. Sections 13–14 passed independent review: the direction and all four
hypotheses are `PARTIAL_OVERLAP`, retaining all four. This is an actual published
novelty assessment against the retrieved corpus, not worldwide novelty assurance.

Existing `novelty/schemas.py`, `novelty/prompts.py`, and `novelty/pipeline.py` now
support verified direct-page signature provenance and exact-input recovery of a
withheld signature. New `novelty/reassessment.py` preserves complete comparisons
while recovering incomplete source/comparison records with located concerns and
fresh reviews. Existing `novelty/assessment.py`, `novelty/assessment_evidence.py`,
and `novelty/assessment_prompts.py` add exact-draft synthesis recovery and actionable
paper/target citation diagnostics without weakening evidence or publication gates.
New `tools/validate_reassessment.py` saves resumable full-run artifacts. Detailed
approach and tests remain in the existing module docs.

Section 15 also ran fully, but its scientific verdict is **withheld**. Its draft
recommended KEEP for the direction/H1–H3 and REFINE for H4; these are unpublished
recommendations. The final route is `resolve_upstream` for H4/E4's reference-group
and effect-interpretation ambiguity, with `scientific_action: null`. The reviewer
also made an overbroad reference-group inference and missed a renewed low-spread
ablation assertion; both limitations are recorded in the Research Critic doc.
Thus the comparison/synthesis recovery implementation and full live run are
complete, while this candidate still needs H4 clarification and fresh scientific
review before section 16. The final bounded scientific cycle was completed; no
third candidate revision was silently started.

Artifacts: `/home/hema/research_runs/momentkv_reassessment_v13/`. Final typed
comparison, assessment, and critic results reload successfully. Regression:
**589 tests plus 13 subtests passed**, one existing faiss/NumPy warning. See
[Novelty Search](novelty_search.md) for module changes and
[Research Critic](research_critic.md) for complete live results and remaining issues.
