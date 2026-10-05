# Novelty Search (`novelty/`)

## 2026-10-04 — Initial implementation: signatures and retrieval

This new module implements the retrieval preparation in architecture sections
9–11. It accepts reviewed directions from `directions_v3`, creates evidence-backed
search signatures at direction and hypothesis level, reviews those signatures,
searches the full configured index and reranks a shortlist for later original-page
comparison. Its output contract is `novelty_search_v1`.

This stage **does not assign novelty verdicts**. Candidate-versus-prior-work
comparison, overlap labels, refinement, critique and portfolio ranking in sections
12 onward remain downstream work. A search with no matches is an empty search
result, not proof of novelty. A relevance ranking is not evidence that two papers
answer the same scientific question.

## Inputs and evidence

The input is a complete persisted `GenerationResult`, not a copied hypothesis
sentence. Its v3 validator requires each direction to equal its linked passing
correctness-review snapshot. Each direction retains the full proposed direction,
hypotheses, suggested tests, rationale, accepted opportunity, selected evidence,
source paper roles, reviewed-page references and artifact hashes.

`NoveltySearcher` reloads the source `paper.json` files and **all original reviewed
page markdown**, using `reasoning.evidence.PaperStore`. It checks artifact hashes,
evidence JSON/provenance paths and values, source locations, required page coverage
and original page hashes before the first model call. The signature context includes
full extracted paper JSON and original source page text, along with the complete
candidate and opportunity snapshot. Extraction fields are context, not independently
verified author claims. No landscape or reasoning stage is rerun.

Additional available source pages can be supplied through `extra_pages` (Python)
or `--extra-pages FILE` (CLI), a JSON mapping such as `{"paper-id": [5, 6]}`.
Only paper IDs already in the candidate's source manifest are allowed. Additional
pages receive hashes in the output context manifest. This is explicit page loading,
not an autonomous page-discovery agent. Missing pages or oversized complete contexts
produce diagnostics; sources are not silently omitted. Appendices are not required.

## Signature representation and review

One model call produces a direction-level target and one target for **every**
hypothesis. Each target contains the architecture's semantic facets:

- problem;
- intervention;
- decision signal, where applicable;
- mechanism;
- regime;
- comparison;
- expected effect.

Each facet records its text and basis: `source_fact`, `candidate_proposal` or
`unknown`. Candidate proposals point to exact fields in the original proposal.
Source facts require known evidence IDs and verbatim original-page quotations;
code checks quote membership and paper ownership. Unknowns retain their provenance.
The independent signature judge checks semantic faithfulness, scope, conditions,
comparators, quantifiers, attribution and useful query coverage against the entire
candidate and source context. It does not assess novelty or scientific merit.

For example, the RAG candidate's proposed comparison of prompts with and without
an answer/refuse decision can be represented as a proposal. The source observation
of zero correct refusals can be represented as a fact with its original-page quote.
An unsupported assertion about what the original prompt omitted must remain unknown.
Acceptance of the upstream draft does not verify that assertion.

```
reload and verify sources
  → create signatures and complementary queries
  → fresh-context signature review
      pass → search every target
      revise → revise once → fresh review → search only on pass
      abstain / error / unresolved → withhold, retain diagnostic
```

A reviewer sees the source context and proposed signature in a fresh two-message
conversation. The second review receives no first-review verdict or revision
feedback. Default reviewer provider/model is the generation provider/model;
`NOVELTY_REVIEW_MODEL`, `--review-model`, or an injected `review_chat` selects an
alternative. This is independent context, not proof of independent model errors.
Review snapshots, reports and failures remain auditable. Creation and substantive revision each allow one format/reference repair,
revalidated before review. Invalid or truncated judge and reranker responses fail
closed without repair. Provider failures and abstentions are not repaired.

## Full-corpus retrieval and reranking

Each target has up to four complementary queries by default. Prompts request both
the combined relationship and less restrictive component/mechanism searches with
alternative terminology. Expected effects are search subjects, not assumed truths.
The code validates nonblank unique queries and complete target coverage.

`research.retrieval.MultiQueryRetriever` executes query searches on a bounded
thread pool. `LocalBackend` uses the existing BM25 + FAISS hybrid search, with the
exact embedding configuration recorded in the index. `OpenSearchBackend` is also
supported through the existing adapter. **No seed-paper, record-type or year filter
is applied.** Original source papers remain eligible matches; they are not excluded
simply because they motivated the direction.

Defaults are 50 records per retrieval channel/query through the existing adapter,
100 candidate papers per target after cross-query reciprocal-rank fusion, then
20 papers after semantic reranking. All raw fused candidates and query/record
provenance are retained. PaperCards are loaded from the retrieved `paper.json`
artifacts and supplied with matching records to the reranker. It must select exactly
`min(rerank_k, available_candidates)` unique IDs from the loaded candidate set and
provide relevance explanations.

The architecture sketches BM25, vector search and PaperCard semantic matches. This
implementation uses the existing hybrid record index for discovery and PaperCards
for semantic **reranking of that candidate pool**. It does not claim to have added
a third independent full-corpus PaperCard vector index. Pool recall is therefore
limited by the existing indexed records and query coverage.

A missing retrieved paper artifact is recorded and excluded from semantic reranking;
the target reports `partial`, retaining the original hits and missing IDs. A failed
query or invalid reranking reports `failed`; the system does not label a raw ranking
as a completed semantic rerank. An empty successful search reports `complete` with
an empty shortlist and `novelty=not_assessed`.

Queries and target searches run concurrently. Candidate generation/review phases
follow their necessary dependencies. LLM calls share a semaphore and atomic attempt
budget; all target retrievals share the caller-owned retriever's bounded pool.
Artifact loads are threaded and synchronized by resource, reusing PaperStore's cache.

## Contracts and files

| File | Responsibility |
| --- | --- |
| `novelty/schemas.py` | Facet provenance, target signatures, review audit records, ranked matches, target retrieval status and `novelty_search_v1` handoff. |
| `novelty/prompts.py` | Separate signature construction, fidelity review and relevance reranking instructions and versions. |
| `novelty/pipeline.py` | Source verification, signature checks, bounded review/revision, asynchronous retrieval and reranking, budgets and run metadata. |
| `novelty/__main__.py` | `uv run python -m novelty` CLI for local or OpenSearch backends; caller-owned backend/client cleanup. |
| `tests/test_novelty_search.py` | Offline evidence, review, concurrency, retrieval-boundary and persisted-contract regression tests. |
| `tools/validate_novelty_search.py` | Bounded live validation with saved upstream inputs, raw API requests/responses, output and summary. |

Existing generator, miner, landscape, reasoner and search implementations are
reused without changes. `directions.judge.validate_report` is reused only for
structural issue-pointer and original-quote checks; the signature-review prompt
is separate. The documentation index and project status receive short additions.

The result binds to the full input generation hash and each direction snapshot
hash. Each accepted signature equals its latest passing review, includes a retrieval
outcome for every target, and retains the exact queries used. Persisted validation
rejects changed queries, missing reviews, invented ranked IDs or inherited search
filters. Ranked-paper artifact hashes are retained for subsequent evidence loading.
This is a provenance contract, not a cryptographic attestation or a semantic proof.

## Settings and usage

Defaults: concurrency 4, LLM attempt cap 40, maximum 4 queries per target, record
candidate depth 50, paper pool 100, shortlist 20, retrieval timeout 120 seconds.
Input and output allowances default to 500,000 tokens each, configurable with
`NOVELTY_MAX_INPUT_TOKENS` and `NOVELTY_MAX_OUTPUT_TOKENS`. Complete prompts, including
schemas, are checked in every LLM phase; actual provider limits still apply.

A clean direction normally costs two signature/review calls plus one reranking call
per nonempty target pool. A direction with four hypotheses normally has five targets,
so seven calls; one signature revision/review adds two. One format/reference repair
per creation or revision can add at most two more calls (eleven total for that
five-target case). Every such repair uses the same atomic attempt budget. Empty or wholly unavailable
pools make no reranking call. Query-embedding API calls belong to the search backend
and are **not included in the LLM attempt counter**. The local backend may issue one
embedding request per query; duplicate query text across targets is not globally
cached by this module.

```bash
uv run python -m novelty \
  --directions /path/to/directions-v3.json \
  --root /srv/research_finder/markdown \
  --index-dir /srv/research_finder/index \
  --out /path/to/new-novelty-search.json
```

Use `--backend opensearch` for the configured OpenSearch index. `--no-vector` selects
an explicitly recorded local BM25-only mode for offline checks; it is not silently
used as a fallback. CLI settings expose the budgets and pool sizes. `NOVELTY_PROVIDER`
and `NOVELTY_MODEL` configure calls, falling back to the shared provider configuration.
The CLI refuses to overwrite an existing output and exits nonzero for withheld
signatures, incomplete/failed retrieval or an empty input direction set. Output
artifacts remain available for diagnosis.

Python service callers inject and reuse their retriever and store; one
`NoveltySearcher` instance is used per run. The module closes only clients it creates.
Record `corpus_id` and `retrieval_mode` when injecting custom adapters. The service
must point them at the full configured corpus; no wrapper can prove that an injected
backend itself was not externally restricted. The run identifies the index path,
not an immutable full-index snapshot; index versioning remains an infrastructure
responsibility.

## Validation

The initial focused suite passes 34 tests, covering full source context, additional
pages, artifact/page changes, quotation ownership, target coverage, fresh review
contexts, bounded revision, independent concurrency, withheld/invalid review,
full-corpus eligibility, empty/missing/failed retrieval, invalid reranking,
attempt/input budgets and persisted handoff tampering.

A read-only BM25 check of `/srv/research_finder/index` on 2026-10-04 found 7,225 papers
and 983,126 records. Two RAG-related queries returned 20 papers with no year or type
filter, including papers outside the original source. It made no external calls;
this validates local index access, not hybrid retrieval or LLM signature quality.
Artifacts: `/home/hema/research_runs/novelty_local_index_check/`.

The live harness uses one saved RAG direction, all four of its hypotheses, full
source extraction/page context, at most two queries per target, pool 20 and shortlist
5. It is bounded to 12 DeepSeek calls and uses the index's configured DeepInfra
`Qwen/Qwen3-Embedding-8B` query embeddings. Upstream generation is not rerun.
Automatic approval review initially blocked the broader payload; the user then
explicitly approved this exact test. Results and manual inspection are recorded
below. No novelty verdict is claimed by this test.


### Initial live contract failure and correction

The first live run, `/home/hema/research_runs/novelty_signature_rag_live`, used one
DeepSeek call and was withheld before review/search. Five `unknown` facets had all
provenance lists empty; the schema correctly rejected them. Usage was 49,760 prompt
tokens and 22,038 completion tokens, including 15,183 reasoning tokens. No embedding
or reranking requests were made in that run.

`novelty/schemas.py` now describes unknown-field provenance explicitly in the model
schema, and `novelty/prompts.py` uses `novelty_signature_v2_provenance`. Unknowns must
point to the candidate field raising the uncertainty, such as `/uncertainties/0`.
Mixed factual/proposed statements must be split into atomic facets with appropriate
basis labels. No validator was relaxed and no failed output was edited into a pass.
The follow-up uses a maximum of 11 calls, preserving the user's aggregate 12-call
approval across both runs. Initial and follow-up artifacts remain separate.


### Reference normalization and bounded signature repair

The next raw response supplied real candidate-field references with the payload's
`/candidate/` prefix, which the initially strict proposal-relative resolver rejected.
`novelty/pipeline.py` now removes that unambiguous prefix before validation and review;
all other pointer validation remains. This changes pointer syntax only, not claims,
basis labels, evidence or queries. The normalized snapshot is what the judge sees
and what the result stores.

Reusing that same saved response locally exposed two source quotations that were
paraphrases rather than original text. They were rejected; no API call was made in
that replay. A bounded format/reference correction path now reports invalid quotes
with their target, facet and page, and allows one correction for initial creation
or a substantive revision. Quotes are still validated verbatim and by paper
ownership; no validator was relaxed. Persistent invalidity prevents review/search.

The live harness supports `--reuse-signature-call FILE`, checking that the complete
saved source/candidate payload equals the current payload before reusing a raw
creation response. This avoids repaying for generation during validation; reviews,
corrections, embeddings and reranking still run live. It records the reused file,
actual API attempts and internal attempts separately. It is a validation harness
option, not a production review bypass. The final replay permits eleven internal
attempts, including that one reused response, so at most ten new real calls: combined
with the two earlier real calls, it remains within the authorized total of twelve.


### Final validation and current limitations

The final full suite passed **276 tests plus 13 subtests**, including **37 novelty
module tests**. `git diff --check` and the CLI help check passed. The existing
FAISS/NumPy deprecation warning remains. Regression coverage now also includes
payload-wrapper pointer normalization before review, successful format repair,
no repair on provider failure/abstention and an explicit JSON-object request
instruction independent of paper contents.

The live sequence made **12 actual DeepSeek attempts in total**, staying within
the approved bound. Original failed artifacts remain unchanged; summary annotations
point to later results rather than relabeling failed runs as successful.

| Artifact directory under `/home/hema/research_runs/` | Actual calls | Result |
| --- | ---: | --- |
| `novelty_signature_rag_live` | 1 | Missing provenance on unknown facets; withheld before review/search. |
| `novelty_signature_rag_followup` | 1 | Payload-prefixed candidate pointers; withheld, raw signature retained. |
| `novelty_signature_rag_reviewed` | 0 | Reused response exposed two non-verbatim quotations; withheld. |
| `novelty_signature_rag_repaired` | 9 | One format repair, review → revision → fresh pass, five completed hybrid searches; one rerank succeeded and four received a provider JSON-mode rejection. |
| `novelty_rerank_json_probe` | 1 | Corrected request successfully reranked a previously failed direction target using its saved hybrid pool; five papers, four outside the seed set. |

The five reviewed targets preserve the complete direction and all four hypotheses.
Manual inspection checked the at-least-two-of-three thresholds in H1/H2, the
per-comparator noise-interval prediction in H3, and H4's two-component comparison
plus below-trace-tuned prediction. Unknown original prompt composition is retained;
the source page facts and proposed evaluation plan are separated following review.
All five targets retrieved 20 papers each through the full hybrid index, with no
seed or year restriction. The first successful hypothesis shortlist included four
papers outside the source set.

Reranking exposed a concrete provider request bug: the old rank prompt did not
explicitly contain the word JSON, which is required for JSON-object response mode.
One target happened to contain that word in its paper context and passed; four
others received HTTP 400. The fix in `novelty/pipeline.py` makes **every** system
message explicitly require a JSON object. `novelty/prompts.py` now records
`novelty_rerank_v2_json`. The final one-call probe exercised the production `_search`
path with the exact previously saved hybrid pool and successfully returned five
valid IDs. Its reproducible `probe.py`, request, response and result are retained
in the probe directory. Three failed hypothesis reranks were not repeated after
this shared fix because the approved call allowance was used; this is not reported
as an all-five-target end-to-end reranking pass.

The direction shortlist includes the original TRACE paper and papers such as
*Knowing Before Answering: Decoding Language Models for Reliable RAG* and
*When RAG Disagrees: Detecting Latent Epistemic Conflict via Logit Interactions*.
These are retrieval candidates based on PaperCards and matched records. Their
actual overlap with the proposed hypotheses has **not** been judged from original
pages in this module.

One attribution limitation remains: the implicit-decision causal explanation is
still worded categorically inside a `candidate_proposal` facet, while original prompt
contents are separately marked unknown. It is not promoted to `source_fact`, but the
requested atomic classification of its unestablished premise as unknown was not
fully achieved. The signature judge is fallible, as is the upstream judge; this
live sample does not establish general semantic correctness or retrieval recall.
The complete inspection is in
`novelty_signature_rag_repaired/semantic_inspection.json`.

The implemented handoff is ready for the next development stage: section 12
candidate-versus-prior-work comparison using original evidence from the shortlist.
That next stage must preserve the source-fact/proposal/unknown distinction and
report search/evidence coverage; it must not infer novelty from an empty result.

## 2026-10-04 — Follow-up: remaining reranks, attribution and retrieval coverage

The user authorized completing the remaining tests with as many API calls as needed.
This supersedes the prior live-validation call bound for this follow-up only; normal
production attempt limits remain unchanged. Existing artifacts are retained.

`novelty/prompts.py` now uses `novelty_signature_v3_conditional_premises` and
`novelty_signature_review_v2_conditional_premises`. The instructions distinguish
an untested intervention prediction from a factual premise about an existing
implementation. A `candidate_proposal` label does not qualify a categorical
“Because A omits X, Y happens” claim. Without source support for A omitting X, that
premise must be unknown and the explanation conditional. Neutral cache/prefetching
examples illustrate unsupported premises, legitimate proposed effects and genuinely
source-supported details. These are semantic instructions, not a runtime keyword
rule specialized to the RAG paper. The signature schema and production call flow
are unchanged.

Additional reproducible tools:

| File | Purpose |
| --- | --- |
| `tools/validate_novelty_reranks.py` | Reuse exact saved hybrid pools and rerank selected targets concurrently, without new embeddings or upstream generation. |
| `tools/validate_novelty_attribution.py` | Live negative/positive controls: the historical unsupported premise, explicit unknown plus conditional explanation, and an original-text-supported detail. |
| `tools/check_novelty_coverage.py` | Compare predeclared related papers against raw pools and reranked shortlists, preserving per-target positions and checking control-source hashes. |
| `tools/validate_novelty_search.py` | Adds explicit query-count, candidate-pool and shortlist parameters so full production-depth checks are reproducible. |

All three previously unrerun targets (`dir-000-h01`, `dir-000-h03`, `dir-000-h04`)
completed with five ranked papers each in three concurrent calls. Artifacts:
`/home/hema/research_runs/novelty_remaining_reranks`. This closes their pending
JSON-request regression check; it does not reapprove the old signature.

The first attribution-control run rejected the exact historical unsupported premise
with original-page support. The intended positive fixtures were not yet clean:
one retained a categorical note-generation/decision-allocation assertion; another
classified an unresolved question as a proposal. Those are additional attribution
issues, not evidence that the judge falsely rejected correct content. The fixture
was corrected explicitly and rerun; both sets of raw controls/reports remain saved
in `novelty_attribution_controls` and `novelty_attribution_controls_clean`.

Before the new coverage searches, related papers were selected from catalog titles
and original first pages. The manifest records selection rationale and page hashes
at `/home/hema/research_runs/novelty_coverage_followup/predeclared_controls.json`.
RAG includes reward-shaped refusal and selective help-seeking; KV includes output
perturbation, attention matching and output reconstruction. No control paper IDs
are injected into production queries. These are targeted coverage checks, not
novelty labels or a statistically representative retrieval benchmark. One RAG
control was seen in the earlier run and is explicitly treated as a regression
control. Adjacent work need not outrank closer papers in every hypothesis shortlist.


The cleaned attribution controls all met their predeclared expectations: the judge
rejected the exact unsupported premise and passed both the unknown/conditional
version and the genuinely source-supported detail. Manual inspection confirmed the
negative verdict identified `/targets/0/mechanism/3/text`, with p6/p8 quotations,
and distinguished observed zero refusals from an unestablished prompt omission.
These paired controls validate the targeted correction without requiring rejection
of legitimate hypotheses. They are not a statistical estimate of judge reliability.

Fresh RAG and MOMENTKV validation uses production-depth retrieval settings:
maximum four queries per target, up to 100 candidate papers, up to 20 reranked papers.
Both are full runs from saved v3 directions through new signature creation, review,
any bounded correction, hybrid retrieval and ranking; no hand-edited signature is
injected into either run. Their artifacts are `novelty_rag_full_live` and
`novelty_kv_full_live` under `/home/hema/research_runs/`.


### Follow-up results: all requested checks completed

The final code passes **276 tests plus 13 subtests**; the prompt changes do not
change deterministic contracts. CLI/help and `git diff --check` pass. The existing
FAISS/NumPy warning is unchanged. This follow-up made **27 actual DeepSeek calls**:
three pending reranks, two three-control attribution runs, and two nine-call full
pipeline runs. Production budgets remain unchanged. Each full run used one creation,
one review, one substantive revision, one fresh review and five reranks, with peak
API concurrency four. There were no provider errors, truncations, missing-artifact
diagnostics or final withheld signatures in either full run.

| Topic | Targets | Retrieved pool sizes (direction, H1–H4) | Shortlist sizes | Source/novelty status |
| --- | ---: | --- | --- | --- |
| RAG | 5 | 100, 92, 96, 64, 95 | 20 for every target | Reviewed signature; novelty not assessed. |
| MOMENTKV | 5 | 90, 63, 100, 88, 61 | 20 for every target | Reviewed signature; novelty not assessed. |

Each target used four queries, searched without corpus restrictions and ranked
19 papers outside its one-paper seed set alongside the seed. Counts are per target;
the same paper can occur in several shortlists. The forty hybrid query searches
use configured DeepInfra embeddings; embedding transport attempts were not separately
instrumented in the DeepSeek call ledger.

Manual inspection confirmed the **fresh, unedited RAG signature fixes the historical
premise error**. Its intervention facet states: “Whether the original Chain-of-Note
or few-shot CoT comparators include an explicit decision step or refusal exemplars
is unknown.” Its mechanism states: “If the decision is left implicit, the model may
default to answering; an explicit decision step might allocate a generation step
to the decision.” This supersedes the earlier open attribution status for the
novelty signature in this tested case. The original upstream Direction Generator
artifacts were not rewritten, and this does not establish universal judge accuracy.
H1/H2 thresholds, H3's per-comparator prediction and H4's two-component comparison
remain intact. The review also corrected primary-measure choices labeled as facts.

MOMENTKV manual inspection confirmed all four effect directions/comparators and
matched-condition requirements remain, high-sigma accuracy behavior stays unknown,
and the Jensen-gap/approximate-weight distinction is preserved. The initial judge
requested precise source spans for equations, numbers and low-sigma conditions;
the revision supplied them and passed fresh review. The signature does not claim
high-sigma failure was established in the source paper.

Predeclared coverage outcomes:

| Related paper | Example pool → shortlist rank | Coverage interpretation |
| --- | --- | --- |
| Reward Shaping for Robust Refusal (`7d99134e9be1`) | RAG direction 7 → 3 | Found in all five RAG targets. |
| MASH / selective help-seeking (`7ff2a18abb9c`) | RAG direction 89 → 9 | Alternate abstention vocabulary found in all five targets. |
| CriticalKV (`a60b8c0468d0`) | KV direction 15 → 2 | Perturbation-based eviction found in all five targets. |
| Fast KV Compaction / Attention Matching (`4d683d7340e6`) | KV H2 100 → 3 | Found only by H2's normalizer-focused search in this run; near the pool boundary. |
| ReST-KV (`3db4139adeef`) | KV H1 2 → 3 | Found in H1–H4, not the direction-level pool. |

All five controls appear in at least one raw pool and shortlist. These results
illustrate why hypothesis-specific retrieval and a wider pool matter; a direction-
only or twenty-paper-only search would miss some related work here. They do not
establish exhaustive recall, statistical accuracy or that these papers invalidate
the hypotheses. The paper at rank 100 is a concrete remaining recall-sensitivity
example, not proof that the entire corpus was exhaustively compared.

Artifacts and precise semantic checks are retained in each full run's
`semantic_inspection.json`; the coverage manifests, per-target ranks and aggregate
call ledger are in `novelty_coverage_followup/`. The pending rerank checks and the
specific signature-attribution regression are closed for these tested cases.
The next implementation stage remains original-evidence candidate-versus-prior-work
comparison (architecture section 12), followed by direction/hypothesis novelty
interpretation and refinement. No extra model calls were used to claim novelty.

## 2026-10-04 — Section 12: original-evidence comparison

This update extends the module beyond the sections 9–11 implementation described
above. `novelty_search_v1` remains the retrieval artifact; the new
`novelty_comparison_v1` artifact adds candidate-versus-prior-work comparison.
Earlier statements that comparison is unimplemented describe the earlier version.
Sections 13–15 (aggregate novelty interpretation, candidate refinement and research
critique) remain downstream; this implementation does not declare a hypothesis novel.

### Added and changed modules

- **New `novelty/comparison_schemas.py`:** eight-dimension comparisons, pairwise labels,
  review snapshots, source manifests and explicit coverage of every shortlisted pair.
- **New `novelty/comparison.py`:** `NoveltyComparator` validates saved direction/search
  lineage, loads original prior evidence, schedules comparisons and independent reviews,
  repairs invalid output and permits one substantive revision followed by fresh review.
- **New `novelty/comparison_prompts.py`:** semantic comparison and independent audit
  rules. No keyword-based novelty or scientific-quality rules are introduced.
- **New `novelty/compare.py`:** separate CLI for this stage; the existing retrieval CLI
  and persisted retrieval schema are unchanged.
- **Existing `novelty/pipeline.py` changed:** the shared call helper now routes
  `review_comparison`, as well as `review_signature`, through the separate reviewer
  callable/model when configured. Signature generation and retrieval behavior stay
  the same; the comparator reuses evidence/context loading and budget infrastructure.
- **New `tests/test_novelty_comparison.py`:** offline contract, source-integrity,
  async execution, incomplete-evidence, review and budget regression tests.
- **New `tools/validate_novelty_comparison.py`:** reproducible live validation using
  saved inputs, raw requests/responses, call events and explicit tested/skipped coverage.

### Inputs and evidence scope

The stage requires the original `directions_v3` and matching `novelty_search_v1`.
It verifies the generation digest, exact candidate digest, passing signature snapshot,
all source artifact/page hashes, signature references, and the retrieved artifact
hashes recorded during reranking. Conflicting prior-paper hashes or mismatched
lineage fail before API work. All prior papers must come from a saved shortlist.

For each shortlisted paper, load its full extracted `paper.json`, every locally
available numeric page markdown file, and any additional page numbers referenced
by structured extraction provenance. `PaperStore` can fetch referenced pages using
its configured fallback. Unreferenced remote pages are not enumerated. Record each
loaded page's hash and any missing referenced pages. This is explicitly
`available_extracted_pages`, not complete-PDF or appendix coverage. No appendix
extraction requirement has been added. Without any original prior pages, withhold
the comparison rather than use extraction summaries as proof.

The model sees the full candidate, original source context, reviewed signatures,
and prior-paper JSON/pages. Signatures govern candidate uncertainty and conditional
premises; unsupported statements in older upstream drafts do not override a corrected
signature. Extraction fields aid navigation; factual prior-work statements require
verbatim original-page evidence, checked for text presence and paper ownership.
Semantic entailment is separately checked by the independent reviewer.

### Comparison behavior and independent review

Compare all eight architecture dimensions: **problem, method, mechanism, signal,
regime, evaluation, scientific question, hypothesis**. Each dimension records the
candidate statement with signature-relative JSON Pointers, prior-work statement,
relation, original quotations and comparison reasoning. Unknown and inapplicable
are distinct. Missing evidence cannot become a finding that the authors did not
study something. Different conditions must remain explicit.

Each direction/hypothesis–paper pair receives `SAME`, `VERY_CLOSE`, `PARTIAL_OVERLAP`,
`ADJACENT`, `DIFFERENT`, or a null classification when evidence is insufficient.
These are overlap interpretations, not literature-wide novelty verdicts. A separate
`hypothesis_tested` field distinguishes tested, discussed-only and not-established
relationships; direction targets can also mark this inapplicable. A hypothesis
classified `SAME` must have direct evidence of the relationship being tested, and
all applicable dimensions must match. An author suggestion is not a tested result;
a negative experimental/theoretical result can still show the question was studied.

Group targets sharing a prior paper into one comparison request, preserving separate
pair outputs. Different papers run concurrently (default four in-flight calls).
Each draft receives a fresh-context independent review of its source support,
attribution, candidate fidelity and label consistency. The reviewer receives no
previous review or generator conversation. The reviewer may use a separately
configured model/client; otherwise a separate call uses the same configured model.
This is independence of context, not guaranteed independence of model errors.

Invalid JSON, references or quotations allow one format/reference repair per creation
or revision. A valid review requesting corrections permits one substantive revision
and a fresh review. Unresolved, abstained, invalid, truncated or failed reviews withhold
the comparison; the rejected draft/review remains in the audit trail. Provider and
budget failures do not silently retry or yield novelty labels. Only a comparison
identical to its last passing review snapshot can be published.

### Coverage, budgets and operation

Every shortlisted pair has a completed comparison or an explicit failed, unresolved
or skipped outcome. Original retrieval status is retained by target. Empty/failed
retrieval, an unresolved comparison, or unknown evidence is never a novelty claim.
`--paper-id` can explicitly select a subset for staged inspection; every omitted
shortlist pair stays recorded as skipped. A successful subset command means the
selected work completed, not that the full shortlist was examined.

Input/output allowances follow `NOVELTY_MAX_INPUT_TOKENS` and
`NOVELTY_MAX_OUTPUT_TOKENS` (default 500,000 each). Complete prompts are measured;
there is no silent text truncation or target omission to fit. Provider hard limits
still apply. Default attempt allowance is 500 calls, counted atomically before each
request; callers can reduce/increase it. Caller-supplied clients remain caller-owned.

```bash
uv run python -m novelty.compare \
  --directions /path/directions.json --search /path/novelty-search.json \
  --root /srv/research_finder/markdown --out /path/novelty-comparison.json \
  --provider deepseek --concurrency 4 --max-calls 500

uv run pytest -q tests/test_novelty_comparison.py
```

The CLI refuses to overwrite an output file and writes all diagnostics alongside
results. No original candidate, source artifact, extraction, or search result is
rewritten. This section is the implementation approach; validation outcomes follow
below when checks complete.

### Section-12 validation status (2026-10-04)

- **42 comparison-specific offline tests passed.** The full suite passes **318 tests
  plus 13 subtests**, with only the existing FAISS/NumPy deprecation warning.
- Covered exact saved-input binding; changed/missing source and prior artifacts;
  all available page loading; explicit missing referenced pages; invented or wrong-
  paper quotes; invalid candidate pointers; missing/duplicate dimensions and targets;
  truncation; bounded repair; independent reviewer client/model routing; fresh review
  after one substantive revision; unresolved-review withholding; call/input budgets;
  persisted-review tampering; empty/failed retrieval; concurrent paper batches;
  explicit subset coverage; and isolation of one paper failure from other results.
- Offline source preflight checked **54 unique shortlisted papers per topic** for
  both RAG and MOMENTKV. All saved paper hashes match, all papers have original page
  markdown, and none of the structured referenced pages is missing. This is an
  artifact-availability check, not an overlap assessment. Report:
  `/home/hema/research_runs/novelty_comparison_preflight.json`.
- **Live section-12 validation is pending.** Automatic approval review rejected both
  prepared DeepSeek runs because prior-paper JSON/original pages broaden the earlier
  approved payload. No comparison API calls ran. A specific authorization request is
  pending; earlier signature/retrieval live results do not count as comparison tests.

Prepared live scope is the RAG seed plus Reward Shaping (`7d99134e9be1`) and MASH
(`7ff2a18abb9c`), and the MOMENTKV seed plus CriticalKV (`a60b8c0468d0`), Fast KV
Compaction (`4d683d7340e6`) and ReST-KV (`3db4139adeef`). These seven papers exercise
shared source methods, different interventions, related mechanisms, and hypothesis-
specific retrieval. Every target for which a selected paper was shortlisted is
compared; all other pairs remain explicitly skipped. Expected inspection questions
are preservation of RAG's unknown original prompt contents and conditional mechanism,
training versus prompting/help-seeking distinctions, and preservation of MOMENTKV's
specific variance/normalizer/order relationships without treating a shared cache
objective as proof those relationships were tested. Exact overlap labels are not
preselected as a presumed scientific ground truth.

The next acceptance step is those live comparisons and source-level inspection of
their outputs. Sections 13–15 should follow that validation, not replace it.

## 2026-10-04 — Live comparison validation and follow-up fixes

The section-12 implementation above was committed as **`4c73e34`** before the live
runs. The user then authorized the seven-paper DeepSeek tests and later explicitly
reauthorized the CriticalKV retest when automatic approval review inconsistently
blocked that same payload. These runs use saved direction/search inputs; they do not
rerun upstream mining/generation or expand retrieval. Original run artifacts and
unsuccessful attempts are retained rather than overwritten.

### What changed after the committed implementation

- **`novelty/comparison.py`:** the initial quote-validation exception named neither
  the field nor the offending quotation. Live repairs consequently repeated errors.
  Validation now reports every invalid quotation and candidate pointer with its exact
  output path and source page. Repairs must copy contiguous original text, preserving
  internal Markdown/LaTeX and punctuation. Matching rules have not been weakened and
  the code does not rewrite model claims or substitute approximate quotations.
- **`novelty/comparison_prompts.py`:** explicitly identifies allowed signature-pointer
  roots, requires candidate pointers to support the actual proposed clause rather
  than merely a related source fact, scopes absence claims in every field to supplied
  evidence, preserves candidate unknowns/hedges, and requests source support for each
  prior-side factual clause. Valid contiguous substrings need not include formatting
  delimiters outside the quoted substring. Prompt history is retained in raw calls;
  the current comparison/review versions are `v3_scoped_evidence`.
- **`novelty/comparison.py`, `comparison_schemas.py`, existing `pipeline.py`:** live
  reviewers also produced inexact quotations. One bounded review-format repair is now
  permitted per review round and routed to the configured reviewer client/model.
  The invalid response and precise error remain under `format_repairs`; the repaired
  report is revalidated. This changes the earlier no-repair-on-invalid-review behavior.
  A persistently invalid review still withholds the result. It adds no extra substantive
  revision loop: at most one comparison revision and a fresh review remain allowed.
- **`tests/test_novelty_comparison.py`:** added regression coverage for reporting all
  bad spans, retaining invalid reviewer output, independent reviewer routing during
  repair, and attempt-budget enforcement. **45 comparison tests pass; the complete
  suite passes 321 tests plus 13 subtests**, with the existing FAISS/NumPy warning only.
- **New `tools/validate_novelty_comparison_controls.py`:** replays two historically
  passed comparison drafts and creates two deliberately false claims that a specific
  candidate hypothesis was tested. Their quotes are real and pass deterministic
  reference validation; detecting the false inference requires semantic review.

### Findings that must not be hidden by an eventual passing run

The first RAG run used six calls: its source-paper comparison passed, but Reward
Shaping and MASH failed their bounded structural/quote repairs. The first KV run used
11 calls: the source-paper comparison passed; ReST-KV and CriticalKV failed structural/
quote repairs; Attention Matching was withheld after its second review contained an
invalid quotation. No failed comparison was published as a novelty result.

The Attention Matching reviewer identified a real method error: the draft attributed
compact-key construction to nonnegative least squares. The paper selects compact keys
first, fits biases with NNLS and values with least squares. Later reviews also found
claims listing methods/metrics that their attached quotations did not establish.
MASH review distinguished an unresolved oracle-helper failure from a solved method.
These are substantive evidence checks, separate from literal quotation formatting.

Both injected false-tested claims were rejected at the affected fields: the RAG
claim invented a matched decision-tag experiment; the KV claim invented a high-sigma
stratified first-order-versus-zeroth-order result. However, **both historically passed
baseline drafts received `revise` on re-review**. The RAG draft contained broad absence
claims; the MOMENTKV draft had candidate-mechanism pointer mismatches and incomplete
support for a prior-side question. These were valid defects, not simply counted as
judge false positives. The initial passes therefore cannot establish correctness or
reviewer reliability, and later retests supersede them. This four-case check is not a
statistical estimate of judge accuracy.

The model can still produce uncited clauses, lose a hedge while paraphrasing a
candidate, or fail a literal quote repair. The strict output boundary withholds such
results; a recorded model pass alone is not a guarantee. Detailed final run outcomes
and the call ledger are recorded below after the pending calls complete.

### Final live outcome: acceptance did not pass

**All live calls completed: 63 DeepSeek calls across initial runs, targeted retests
and the four judge controls.** No embedding calls were needed. The seven selected
papers cover 30 target–paper pairs; the other 170 pairs from the two saved 100-pair
shortlists were not tested. This is explicitly a selected-paper validation.

Latest outcomes (earlier passes do not override later failed checks):

| Paper | Target pairs | Latest automated outcome | Remaining issue |
| --- | ---: | --- | --- |
| Conflict-aware RAG source | 5 | Withheld after revise → revise | Citation coverage, overly broad absence wording and candidate-pointer support. |
| Reward Shaping | 5 | Withheld after revise → revise | Two hypothesis-side prior claims lack attached support; candidate unknowns omitted. |
| MASH | 5 | Withheld after revise → revise | Uncited prior-side factual clauses and an unsupported candidate condition. |
| MOMENTKV source | 5 | Withheld after revise → revise | Missing support, broad absence wording and loss of proposed-mechanism hedges. |
| CriticalKV | 5 | Withheld after revise → revise | Two uncited prior clauses, a candidate-pointer mismatch and omitted candidate unknowns. |
| Fast KV Compaction / Attention Matching | 1 | Failed bounded quote repair | The generated p8 log-perplexity quote is not an exact original substring. |
| ReST-KV | 4 | Passed after revise → pass | ADJACENT; exact candidate hypotheses remain not established. Manual wording caveat below. |

Thus the automated boundary publishes **4 of 30 selected pairs** and withholds 26.
This does **not** establish clean manual acceptance of the four: targeted inspection
of ReST-KV also found unqualified “does not test” wording in overall rationales,
despite scoped non-establishment language in its uncertainty fields. The mechanism
and effect-direction distinctions are useful, but that wording still needs correction.
No novelty verdict or novelty percentage follows from these results.

The final CriticalKV retest used five calls and completed after the user's explicit
reauthorization. It still returned revise after its one substantive revision. There
is no approval blocker or running comparison process left; the remaining limitations
are output quality and source support. The added reviewer-format repair has offline
regression coverage; the final live runs did not exercise that new branch, so no live
success claim is made for it.

Full aggregate ledger and latest-result mapping:
`/home/hema/research_runs/novelty_comparison_validation_summary.json`.
The reproducible aggregation script is
`/home/hema/research_runs/summarize_comparison_validation.py`.
All twelve run directories, raw requests/responses, failed drafts and review snapshots
are retained under `/home/hema/research_runs/novelty_comparison_*`.

**Next required work remains within section 12.** Improve evidence construction so
candidate descriptions preserve reviewed text/uncertainty directly, and prior claims
are built from individually supported source passages instead of repeatedly generating
broad prose and trying to repair it. Retest those concrete failure cases. The green
321-test offline suite verifies contracts, withholding, budgets and concurrency; it
does not demonstrate reliable model output. Section 12 is not ready to be treated as
validated input to aggregate novelty/refinement in sections 13–15.


## 2026-10-04 — Reference-based section-12 evidence construction (v2)

This changes the comparison implementation described above, preserving those entries
as history. The architecture remains section 12: eight dimensions, separate direction
and hypothesis targets, selected-paper coverage, and scoped overlap. No novelty
aggregation, hypothesis refinement or appendix extraction is introduced.

### Representation and execution

The model no longer writes a second candidate description or copies quotations.
`novelty/comparison_evidence.py` (new) partitions every available extracted page into
blank-line blocks, retaining all non-whitespace characters, Markdown, equations and
tables. A passage ID binds paper/page identity, page SHA-256, character offsets and
exact text. Repeated paragraphs at different offsets receive different IDs. Source
pages remain read-only. Paragraph selection is evidence addressing, not retrieval or
relevance filtering: the complete extracted text remains available to the model.

The reviewed `TargetSignature` is copied unchanged into the result. `candidate_view`
provides deterministic dimension-to-facet references and carries all unknown facets.
Conditions and hedges therefore survive without asking the model to repeat them in
every comparison field. Interpretations can still contradict those conditions; that
is a semantic defect for the independent review to catch, not something text copying
alone can prevent.

The model creates a small shared list of atomic prior claims. Each has a kind
(method, evaluation, result, discussion or explicit author-stated noncoverage) and
one or more passage IDs. Each dimension references claim IDs and supplies its scoped
interpretation. `tested` requires a result claim; `discussed_only` requires a discussion
claim. These structural rules do not prove that the claim follows from its passages
or that the exact candidate relationship was tested. A fresh independent reviewer
checks those questions, attribution, effect directions, conditions and classification.
The reviewer also cites passage IDs, rather than generating quotations.

`render_comparison`/`PaperComparison.evidence_view()` resolve every claim reference
into exact source text alongside the unchanged candidate. Unknown candidate properties
are preserved separately from additional comparison-specific uncertainties. Missing
tests mean **not established in supplied evidence**, not a claim of global absence.
Only the exact draft accepted by the latest valid independent review is published.

Per-paper calls remain asynchronous. All shortlisted targets for a paper share one
claim set and comparison context. The existing one substantive revision plus fresh
review limit remains; malformed generation and review outputs each have their existing
bounded format-repair allowance. Failures remain explicit, with no novelty verdict.

### Modules changed and artifact compatibility

- **New `novelty/comparison_evidence.py`:** stable source addressing, exact candidate
  views, resolved evidence rendering, comparison/reference and report validation.
- **Changed `novelty/comparison_schemas.py`:** `novelty_comparison_v2`; added `Passage`,
  `PriorClaim`, `ComparisonReport` and reference-based issue records; replaced copied
  candidate/prior statements and quotes with preserved targets and shared claim IDs.
  Persisted targets must equal the parent reviewed signature; passage versions must
  match the page manifest. Published output must equal its passing review snapshot.
- **New `novelty/comparison_legacy.py`:** frozen v1 schemas for reading earlier audits.
  No silent migration or inherited approval of old outputs. External consumers must
  choose the reader for the artifact's explicit schema version. V2 consumers use
  `PaperComparison.evidence_view()` when they need resolved human-readable evidence.
- **Changed `novelty/comparison.py`:** builds the passage catalog from existing
  `PaperStore` pages and preserves the candidate; uses reference-based semantic reports.
  Source/candidate hash checks, budgets, withholding and concurrent scheduling remain.
- **Changed `novelty/comparison_prompts.py`:** comparison/review prompt versions
  `v4_reference_claims`, distinguishing atomic facts from comparison interpretation,
  with no repeated candidate prose or quotation generation.
- **Changed `novelty/pipeline.py`:** retains the follow-up reviewer-format-repair
  routing described above. Signature creation/retrieval behavior is unchanged here.
- **Changed `tools/validate_novelty_comparison.py`:** writes `evidence_views.json` as
  well as raw calls, result, call ledger and coverage summary.
- **Changed `tools/validate_novelty_comparison_controls.py`:** v2 paired re-review
  controls inject unsupported experimental claims with valid passage references.
- **New `tools/replay_novelty_comparison_failures.py`:** offline replay of saved v1
  contexts, testing source-character coverage, exact candidate/unknown preservation,
  and rejection of the retired copied-prose fields. This is not a semantic reapproval.
- **Changed `tests/test_novelty_comparison.py`; new
  `tests/test_novelty_comparison_evidence.py`:** adapted existing gate tests and added
  passage identity/coverage, candidate hedge preservation, wrong-paper rejection,
  exact rendering and discussion-versus-result regressions.

### Validation at implementation time

The complete local suite passes **331 tests plus 13 subtests** (one existing FAISS/
NumPy deprecation warning). A regression exposed and fixed loss of the final paragraph
when page text ended with whitespace. All source characters are now covered.

Offline replay used seven distinct saved paper payloads from the previous runs. All
original target fields and unknowns survived exactly; all non-whitespace source text
remained addressable. It identified 75 historical inexact-quote occurrences and rejected
1,176 historical v1 dimension outputs under the new schema. These are repeated output
occurrences across attempts, not independent scientific examples or accuracy estimates.
Report: `/home/hema/research_runs/novelty_comparison_v2_replay.json`.

Live validation uses the same seven selected papers and 30 target–paper pairs, with
fresh output directories `novelty_comparison_v2_rag_live` and
`novelty_comparison_v2_kv_live` under `/home/hema/research_runs`. Final live outcomes
and semantic-control results are recorded below when complete; passing local tests
alone does not establish scientific acceptance.

### Completed v2 live validation: representation improved; semantic acceptance remains incomplete

All calls finished: **37 DeepSeek calls** (15 RAG comparisons/reviews, 18 KV
comparisons/reviews, four paired judge controls), with peak concurrency three per
comparison run and four for controls. No embedding calls or new corpus extraction
were needed. The same seven papers cover 30 pairs; the other 170 pairs remain skipped.
There were no failed passage-ID/quotation-copy repairs in these runs. Seven generation
format repairs removed unused claims; all seven reached semantic review. Keeping
unused claims out of the shared set still costs repair calls and should be stated
more explicitly to the generator in a future prompt revision.

| Paper | Pairs | Automated outcome | Evidence finding |
| --- | ---: | --- | --- |
| Conflict-aware RAG source | 5 | revise → pass | Added the passage establishing held-out benchmark status. Later control/manual issues below prevent clean acceptance. |
| Reward Shaping | 5 | revise → pass | Missing evidence for the candidate's hypothesis now yields UNKNOWN rather than DIFFERENT; reward training versus prompting remains an affirmative method distinction. Manual scope caveat below. |
| MASH | 5 | revise → revise; withheld | Oracle-helper result cites the collapse within 50 steps but omits the setup paragraph establishing gold answers and all variants. |
| MOMENTKV source | 5 | pass | Separates discussed Jensen self-regulation from empirical high-sigma testing. Later review discrepancy and scope caveat below. |
| CriticalKV | 5 | revise → revise; withheld | Evaluation claim includes 13 RULER tasks and 40% cache size without the paragraphs supporting those two details. |
| Fast KV Compaction / Attention Matching | 1 | revise → revise; withheld | Claim about key selection includes aggregation and NNLS-refitting details not supported by its selected passages. |
| ReST-KV | 4 | revise → pass | Added exact model-identifier support; reconstruction and smoothing comparison remains ADJACENT, with the four candidate relations not established. |

The automated gate publishes **19/30 pairs**, versus 4/30 in the earlier latest
results. This is a representation-and-gating outcome, **not a scientific accuracy
rate**. The three unresolved papers retain their rejected drafts and reports and
publish no comparison. They stopped at the existing revision bound; no repeated
attempts were used to replace these failures with a convenient passing result.

#### Judge controls and source inspection

Both injected false-tested claims were rejected at their affected fields: the RAG
mandatory-decision-tag experiment and the MOMENTKV sigma-stratified approximation-order
experiment. The injected claims used real passage IDs and passed structural validation,
so this exercised semantic checking. However, both previously passed baseline drafts
received `revise` when reviewed again. Thus only **two of four expected control outcomes
matched**; this is not a judge-reliability estimate or an overall passing control suite.

Inspection distinguished real defects from a reviewer error:

- **RAG baseline:** re-review correctly flagged paper-wide non-testing assertions and
  dimension rationales whose local claim IDs did not establish their trace-supervision
  facts. Separately, claim `prior-14` says broadly that trace supervision does not improve
  answer quality, whereas its p7 source qualifies the null by prompt conditions and
  reports improved grounding under sparse instructions. That lost qualifier was missed
  in both review passes. Candidate text is now preserved mechanically, but prior-claim
  interpretation can still overgeneralize source findings.
- **MOMENTKV baseline:** the control reviewer says claim `c13`'s selected passages do
  not mention LongBench. The selected p9 passage `60ca1bf96321` explicitly compares
  margins with LongBench. Directly citing the LongBench setup would improve precision,
  but the reviewer's stated no-mention premise is false. Do not count this automatically
  as a correctly detected defect. Manual inspection separately found unqualified
  “does not test” wording in some pair rationales despite scoped wording elsewhere.
- **Reward Shaping:** the training-versus-prompting method distinction is supported,
  but H3's overall rationale says the prior paper does not use conflict labels or
  conflict-aware prompting, an unqualified absence assertion that its pass missed.
- **ReST-KV:** targeted inspection supports the reconstruction-error, temporal EMA
  and adaptive spatial-smoothing distinction. The four hypotheses remain unestablished
  in supplied evidence. No additional defect was identified in that targeted inspection;
  this does not claim an exhaustive scientific audit.

**Section 12 still lacks clean semantic acceptance.** Stable IDs solve copying and
reference integrity, and unchanged targets solve candidate rewrite drift; they do not
prove that every clause in a prior claim follows from its selected paragraphs. The
remaining work is clause-level claim support/qualification and consistent scoped
interpretation, including reviewer false positives and missed defects. A passing
model report must not be used to claim novelty or to advance these validation examples
as scientifically verified input to sections 13–15.

The exact ledger, run paths, all outcomes and manual observations are saved in
`/home/hema/research_runs/novelty_comparison_v2_validation_summary.json`.
Reproduce it from the repository root with:

```bash
uv run python -c "import runpy; runpy.run_path('/home/hema/research_runs/summarize_comparison_v2_validation.py', run_name='__main__')"
```

This also verifies that the frozen v1 reader can still read all eleven saved historical
comparison artifacts. The v2 result round trips and resolved evidence views were checked
by both live harnesses. `novelty.compare --help` and `git diff --check` pass. The earlier
331-test/13-subtest result remains the final full-suite result; subsequent edits were
validation tools and documentation, with those tools executed on the real artifacts.

## 2026-10-04 — Evidence-first relationship comparison and proposal alignment

This implements the consolidated section-12 plan discussed after the v2 validation.
It changes evidence construction and review inside the existing novelty module. The
eight overlap dimensions and direction/hypothesis separation remain. Earlier entries
are retained as implementation/test history, not overwritten by this design.

### Purpose and decision boundaries

The question is what this prior paper already investigates or establishes about the
candidate's proposed contribution. A negative or inconclusive experiment still studies
a relationship; theoretical establishment does not require an experiment. An inferred
implication is our deduction, with explicit premises, steps and established assumptions,
and is never presented as an experiment or an author-stated result. A bound on an
approximation's error alone does not establish an accuracy prediction. Conversely,
a different benchmark, model or metric does not automatically make the relationship new.

Evidence that establishes the candidate's motivating problem is not sufficient to mark
its proposed intervention/relationship directly investigated. The live rollout exposed
this exact error on the RAG direction, despite an evidence review and an interpretation
review passing it. Positive coverage now needs a `ProposalMatch` for every proposed
intervention, comparison, regime and expected-effect facet. References are checked by
code; the reviewer checks scientific equivalence, including whether a changed setting
matters. Effect sign is assessed separately so a contradicting result can still cover
the relationship. Partial correspondence stays related or unresolved. This applies to
directions as well as individual hypotheses.

### Flow and bounds

1. Build one deduplicated context for a prior paper and all its shortlisted targets.
   Preserve reviewed target text/basis/provenance. Keep every available original prior
   passage and candidate-source pages not already present among those prior pages.
   Use structured paper data for page navigation and immutable artifact validation;
   omit repeated upstream opportunity/paper dumps and duplicated candidate views from
   API requests. The input/output allowances remain unchanged.
2. Extract relevant `PriorClaim` records plus one `Relationship` per target. Map prior
   intervention, comparator, conditions, outcome and conclusion to claim IDs. Keep
   material qualifications and distinguish empirical results, theoretical results and
   discussion. The record is a candidate-specific evidence selection, not a generic
   paper summary or a claim of scientific truth.
3. Independently review **every claim** for support and **every target** for mapping
   and omitted evidence that could overturn the proposed distinction. All extracted
   prior passages remain available. A selective but individually true claim set can
   therefore receive a coverage correction. Unsupported objections may be disputed
   in a source-backed revision; the next audit is fresh and does not inherit the old
   review narrative. Both the objection and revision notes remain in the artifact.
4. Permit one evidence revision and re-audit. Proceed only with supported claims and
   targets whose relationship dependencies and coverage audit are adequate. Retain
   unresolved checks in history. A nonessential unsupported claim can be excluded;
   an unresolved material dependency blocks its target, not all unrelated targets.
5. Generate interpretations across the eight dimensions. The model returns pairs only;
   code supplies the reviewed claims and the relationship/testing status. It cannot
   replace claims while writing the final comparison. If original passages expose a
   material omission, the model can request evidence reopening with target/passage IDs.
6. Independently review interpretation, including factual premises, candidate conditions,
   meaningful scientific overlap and scope language. A grounding/attribution objection
   can reopen evidence, once, before regenerating and re-reviewing interpretation.
   Evidence snapshots are versioned; conclusions cannot use a superseded version.
   There is at most one interpretation revision/reopen cycle. No unbounded consensus
   or repeated retry-until-pass loop is added.

Initial extraction/revision calls and review calls each retain one bounded format repair.
There are at most three evidence snapshots (initial, evidence repair, comparison reopen)
and two interpretation attempts. Shared attempt/input/output budgets still apply. Papers
run concurrently; source loading also runs concurrently. Dependent stages within a paper
are sequential. No per-claim API fan-out is introduced.

### Output and uncertainty

The artifact separates overlap from evidence/review status. `PaperComparison.outcomes()`
returns one outcome for every requested target, including withheld and skipped targets:

- `reviewed`: accepted scoped comparison, with its coverage and effect result;
- `no_matching_result_found`: no matching result established in inspected evidence;
- `insufficient_evidence`: an accepted assessment that material evidence is insufficient,
  with a null overall overlap classification;
- `review_unresolved`: evidence/review could not establish a reliable comparison;
- `not_assessed`: outside explicitly selected scope.

Every outcome retains `literature_novelty: not_assessed`, inspected-evidence scope and
missing referenced pages. Code renders the missing-match statement consistently.
Free-text reasoning is still reviewed for unqualified absence assertions; the structured
status is not a license for contradictory prose. Sections 13–14 must not interpret
missing evidence or review failures as novelty, and handle coverage across multiple papers.

A paper may be `partial`. Only exact independently accepted pairs are published. Claim
issues block their dependent pairs; localized pair issues block the affected pair.
Unlocalizable objections or an abstaining interpretation reviewer withhold the relevant
batch. If a local revision call fails while earlier unaffected pairs remain accepted
against the current evidence version, those pairs are retained. Changed or failed new
evidence does not silently reuse stale acceptance.

### Modules and compatibility

- **New `novelty/comparison_records.py`:** evidence claims, scientific relationships,
  implications, proposal-facet matches, per-claim support/per-target coverage reports,
  versioned evidence audits and revision notes.
- **New `novelty/comparison_workflow.py`:** evidence generation/audit with bounded repairs,
  exact audit coverage validation, eligible-target selection, proposal anchoring,
  dependency-based partial publication, evidence rebinding and deterministic outcomes.
- **Changed `novelty/comparison.py`:** runs evidence review before interpretation,
  deduplicates API context, supports bounded source reopening and retains partial results.
- **Changed `novelty/comparison_schemas.py`:** final artifact version
  `novelty_comparison_v4`. Comparison responses contain interpretation pairs or explicit
  evidence requests, not mutable prior claims. Persisted drafts bind to evidence versions,
  claims and relationship states; accepted projections and proposal anchors are validated.
- **Changed `novelty/comparison_prompts.py`:** final comparison version
  `v5_2_proposal_alignment`; evidence version `v1_2_proposal_alignment`. Instructions cover
  clause qualification, omitted counterevidence, theory/implication, actual proposal
  coverage, scope and reviewer disagreement. Initial v3 live artifacts retain their
  original prompt/schema metadata and raw calls.
- **Changed existing `novelty/pipeline.py`:** evidence audit and evidence-review repair
  tasks route to the configured independent review client/model. Upstream signature,
  retrieval and generation contracts are unchanged.
- **New `novelty/comparison_legacy_v2.py` and `comparison_legacy_v3.py`:** historical
  artifact readers; existing `comparison_legacy.py` retains v1. No old result is silently
  promoted to a new-schema reviewed output. Choose the reader by `schema_version`.
- **Changed `tools/validate_novelty_comparison.py`:** saves evidence audit summaries,
  per-target outcomes and resolved views for incomplete as well as complete papers.
- **New `tools/validate_novelty_evidence_controls.py`:** constructed boundary cases and
  saved real-paper controls for support, omission, negative results, theoretical coverage,
  unsupported implications, qualifier loss and background-versus-proposal confusion.
  It uses the production bounded evidence-review repair path and records raw calls.
- **Changed historical `tools/validate_novelty_comparison_controls.py`:** reads v2 drafts
  explicitly and reuses their saved reviewer prompts so historical controls remain
  reproducible after the production prompt changes.
- **Changed existing comparison tests; new `tests/test_novelty_relationships.py`:**
  stages/budgets, separate reviewer routing, omission repair, disputed objections,
  theoretical/negative-result statuses, implication assumptions, proposal anchoring,
  missing evidence, dependent withholding, local repair failure, bounded reopening,
  persisted version/claim/projection checks and concurrent paper execution.

Final test counts, selected-paper results, controls and remaining limits follow after
all live calls complete. Automated approval is not scientific ground truth; source
inspection and counterexamples remain necessary parts of this validation.

### 2026-10-04 — Final evidence-first validation results

The preceding implementation is now exercised offline and live. **355 tests plus
13 subtests passed**; the only warning was the existing FAISS/NumPy deprecation.
Saved outputs validate with their appropriate readers: four v3 comparison artifacts
and two v4 artifacts. No historical artifact was rewritten into a new-schema result.

This evidence-first validation used **85 additional DeepSeek calls**. This count is
separate from the earlier 63-call and 37-call validation entries above. All calls have
finished. The complete machine-readable ledger is
`/home/hema/research_runs/novelty_comparison_evidence_validation_20261004.json`.
Each run directory below is under `/home/hema/research_runs/` and retains its raw
requests/responses and summary; comparison runs also retain result and evidence views.

| Run directory | Calls | Outcome |
|---|---:|---|
| `novelty_comparison_v3_rag_live` | 17 | Source RAG and MASH completed; Reward Shaping failed response-format repair. |
| `novelty_comparison_v3_kv_live` | 23 | CriticalKV, ReST-KV and AttentionMatching completed; MOMENTKV failed evidence-kind validation. |
| `novelty_comparison_v3_reward_retest` | 4 | Explicit response/state instructions added; five pairs completed. |
| `novelty_comparison_v3_moment_retest` | 6 | Five pairs passed automated review; manual inspection then found overstated proposal coverage. |
| `novelty_comparison_v3_evidence_controls` | 10 | Six constructed cases matched expectations; four real-source cases failed structural review validation. |
| `novelty_comparison_v3_evidence_controls_repair` | 5 | Production bounded review repair used; three real cases matched, one ambiguous negative control did not. |
| `novelty_comparison_v3_grounding_control` | 1 | Explicit loss of the sparse-prompt grounding condition correctly rejected. |
| `novelty_comparison_v4_controls` | 11 | All eleven final controls matched expectations; no format repair needed. |
| `novelty_comparison_v4_rag_source` | 4 | All five source-RAG pairs completed; background evidence no longer counted as testing the proposal. |
| `novelty_comparison_v4_moment_source` | 4 | All five source-MOMENTKV pairs completed; discussion no longer counted as testing the proposed contrast. |
| **Total** | **85** | Failures, corrections and superseded results retained. |

The latest selected results cover the same **seven papers / 30 target–paper pairs**:
RAG source (5), Reward Shaping (5), MASH (5), MOMENTKV source (5), CriticalKV (5),
ReST-KV (4), AttentionMatching (1). All 30 have an automated accepted comparison in
the latest selected artifacts. **170 other shortlisted pairs remain unassessed**;
do not add the skipped counts from targeted retries. Only the two source papers
were rerun with final v4 proposal anchoring; the other five retain v3 outputs.
This is not a claim that all seven papers passed a complete final-v4 live run.

The final eleven controls cover: a negative result that still investigates the
hypothesis; omitted matching negative evidence; a matching theoretical result;
an unsupported theory-to-accuracy implication; the same investigated relationship
on a different dataset; discussion mistaken for a result; background evidence
mistaken for the proposed direction; explicit grounding-condition loss; a supported
qualified null result; a supported LongBench mention; and a fabricated matched
prompt experiment. Both valid evidence and deliberately wrong interpretations are
included. These are fixed checks, not an estimate of population judge accuracy or
a benchmark of how often generated hypotheses are novel.

The historical negative control “Trace supervision does not improve answer quality”
is **ambiguous**, because the source uses that wording for single-truth recall while
separately reporting sparse-prompt grounding gains. The reviewer accepted it with
that interpretation. Its original nonmatching result remains saved; it is excluded
from the eleven final controls. The replacement explicitly asserted that grounding
never improves even with sparse instructions; the reviewer correctly rejected that
assertion. Earlier wording treating the broad answer-quality control as an
unambiguous judge failure is qualified by this finding.

### What source inspection established, and what it did not

The v3 RAG record incorrectly labeled the direction directly investigated because
the paper established the motivating prompting deficit. Its proposed decision-tag,
refusal-exemplar and prompting-only oracle interventions were not established as
reported experiments. The v4 record now reports `related` / `not_assessed` for all
five targets and preserves the neighboring oracle experiment on fine-tuned models.
The final injected background-versus-proposal control also rejects the same mistake.

The v3 MOMENTKV record overstated the paper's theoretical mechanism as direct coverage
of the proposed diagnostic and H2 contrast. The v4 record reports `discussed` /
`not_assessed` for the direction and H2, and `related` / `not_assessed` for H1, H3
and H4. It retains the Jensen lower bound, self-regulation discussion, reported
sigma values and existing ablations without claiming those establish the proposed
high-sigma comparison against less-biased or exact normalizers. Positive coverage
now requires supported anchors for every proposed relationship facet.

The omission audit also improved recognized overlap in AttentionMatching by adding
the attention-mass weighted mixture and the no-bias versus fitted-bias ablation.
MASH's revised oracle claim includes the gold-answer setup and collapse result.
These show why reviewing omission matters in addition to checking individual claims.

**Remaining precision limitations are not hidden by automated acceptance:**

- Historical AttentionMatching prose still contains a paper-wide sounding absence
  assertion even though structured scope is restricted to inspected pages. Downstream
  consumers must retain the structured evidence scope; this prose is not a certified
  absence finding.
- In final MOMENTKV output, claim C14 combines ranking on both models with numerical
  margins that the source gives for LLaMA, without explicitly restricting those
  margins to that model. C15 cites table values but omits the separate caption that
  supplies setup conditions. Historical CriticalKV claim c10 similarly omits the
  separate caption supplying its 40% setting. The full supplied pages contain these
  conditions, but individual attached citations and qualifications remain imperfect.
- These inspections are targeted, not an exhaustive adjudication of every clause in
  all thirty comparisons. A fresh LLM review is fallible, including when it agrees
  with an earlier one. The tests establish the implemented safeguards and observed
  corrections; they do not establish scientific truth or literature-wide novelty.

No appendix extraction or completeness requirement was added. Paper-reported bounds
remain paper-reported evidence; an appendix reference in the source does not mean
its proof was extracted or independently verified. Input/output allowances remain
500,000. Independent papers and controls run concurrently; evidence generation,
evidence review and interpretation within a paper have necessary dependencies.
Sections 13–14 remain downstream work and must consume review status, relationship,
source scope and uncertainty alongside overlap labels.

## 2026-10-04 — Explicit result-scope checks and table-context references

This follow-up addresses the specific qualifier and caption defects recorded above.
All extracted pages were already supplied to the evidence reviewer; those defects
were failures of claim qualification and reference attachment, not missing retrieval.
The architecture and stage boundaries remain unchanged: section 12 records what
prior work establishes; sections 13–14 assess and refine novelty across that evidence.
A change of model or dataset does not automatically imply a new scientific relationship.

### Implementation changes

- **`novelty/comparison_records.py`:** adds `ScopeCheck` and explicit `scope_checks` /
  `scope_summary` on each claim review. Checks identify a concrete result, model,
  comparator, dataset, regime, measurement, assumption or other relevant assertion,
  with exact evidence IDs and `supported`, `missing_citation`, `overstated` or
  `unresolved` status. Conditions are selected for relevance, not a mandatory list
  of every conceivable experimental detail.
- **`novelty/comparison_workflow.py`:** production audits require those fields.
  Result, evaluation and theoretical-result claims need at least one substantive
  check. A supported scope assertion must cite evidence attached to the claim;
  a claim cannot be supported if any of its scope checks reports a defect. Defects
  enter the existing bounded evidence-revision path. Dependency-based withholding
  and reassessment remain unchanged. These are structural consistency checks;
  semantic completeness and source entailment still require the reviewer.
- **`novelty/comparison_evidence.py`:** adds deterministic `table_context_refs` and
  `attach_table_context`. A clearly adjacent, explicitly labeled caption and trailing
  note are attached to cited Markdown tables. Table headers are already inside the
  original table passage. An upcoming table's caption is not attached to the previous
  table. Remote setup paragraphs, ambiguous associations and cross-page references
  are not guessed; the extractor must cite them explicitly. Existing passage IDs,
  text and offsets remain unchanged. Attachment is idempotent and occurs before audit.
- **`novelty/comparison.py`:** supplies the small table-to-context reference map in
  the shared payload. All original passages remain available without duplication.
- **`novelty/comparison_prompts.py`:** explicitly separates a both-model ranking
  from model-specific margins and requests evidence for material conditions. The
  reviewer records scope checks rather than relying on one blanket support decision.
  Missing citations and overstated claims are distinct defects. Scientific overlap
  is assessed separately from exact model/dataset equality. Prompt versions are
  `novelty_comparison_v6_result_scope`, `novelty_comparison_review_v6_result_scope`
  and `novelty_evidence_v2_result_scope`.
- **`novelty/comparison_schemas.py`:** current artifacts use
  `novelty_comparison_v5` and validate the explicit scope audit when reloaded.
  **New `novelty/comparison_legacy_v4.py`** preserves the previous artifact reader.
  Older records may lack scope checks; they are readable as historical outputs,
  not silently promoted to outputs that passed the new check.
- **`tests/test_novelty_result_scope.py`:** adds regressions for caption/header/note
  association, the next table's caption, unsupported or unattached scope evidence,
  contradictory support decisions, persisted missing audits, and correction before
  interpretation. Existing comparison fixtures now include explicit scope checks.
- **`tools/validate_novelty_evidence_controls.py`:** adds saved MOMENTKV/CriticalKV
  claims with missing/restored captions, mixed/correctly qualified model results,
  an explicit wrong-model numerical claim, a different-model/same-relationship
  case and a material stationary/changing-workload difference.

No additional judge, per-claim API fan-out, exact-setup novelty rule or new pipeline
stage is introduced. Independent papers and controls run concurrently. The same
bounded repair limits and 500,000-token allowances apply. Incorrect retained claims
must be corrected even when their correction leaves the overlap label unchanged.

The full offline suite passed **364 tests plus 13 subtests** (one existing FAISS/NumPy
warning). Four historical v3 and two v4 comparison artifacts also reload successfully
with their respective readers. Live outcomes and source inspection are recorded below
when the targeted runs finish; offline success is not a substitute for those results.

### Refinements discovered during this follow-up's live checks

The first scope implementation treated a supported scope check citing an unattached
but known passage as a malformed review. That was the wrong repair category. The
current `scope_citation_gaps` derives missing references from the review and record;
they exclude the affected claim from eligibility and enter the existing substantive
evidence-repair/re-audit cycle. The original model report is preserved unchanged.
The repair request includes the explicit missing passage IDs. If the gap survives
the bounded repair, dependent comparisons remain withheld. Unknown IDs and invalid
schemas still use the bounded format-repair path. Tests cover both successful
citation repair and a gap that remains unresolved.

The reviewer also initially treated the ambiguous original mixed-model claim as
acceptable under a charitable reading. The final instructions require clarification
when a material numerical qualifier is missing, even if an interpretation could be
true. The scope-check explanation must not silently supply a qualification absent
from the claim itself. Final prompt versions are
`novelty_comparison_v6_2_result_scope`,
`novelty_comparison_review_v6_2_result_scope`, and `novelty_evidence_v2_2_result_scope`;
these supersede the initial versions in the preceding implementation note.

Adversarial reviews that bypassed automatic caption attachment sometimes still
accepted a table claim without its caption. Production therefore also validates
that every evidence record retains all clearly associated table context before
interpretation and when loading a v5 artifact (`validate_record(require_context=True)`).
The original missing-caption inputs are additionally exercised through the actual
`attach_table_context` step. This guarantees attachment for the supported adjacent
caption/note patterns; it does not claim semantic detection of every remote setup
passage or every possible Markdown table layout. No historical v4 record is silently
upgraded or subjected to the new persisted requirement.

The real MOMENTKV layout exposed an additional attachment bug during validation:
Table 5's caption is followed by a separate `Baseline: SnapKV.` block before its
body. The initial immediate-neighbor rule could attach Table 6's heading instead.
The final rule follows an explicit **preceding** caption through short labeled setup
blocks to its next table, stopping at another caption, unrelated prose or a page
boundary. It does not attach a following table heading to the preceding table.
A regression reproduces the two caption/setup/table sequences. Against the actual
saved pages, Table 5 now resolves to `p10:38de7bdbf130` (caption) and
`p10:3b90267f908a` (baseline), while CriticalKV's table resolves to
`p6:d0b67d1cbaae` (Ruler, 40% cache).

`tools/validate_novelty_comparison.py` now optionally accepts
`--reuse-extraction-from DIR` for targeted recovery. It validates saved extraction
responses and checks exact prior-artifact hash, targets, prior passages and candidate
source passages before reuse. The saved live extraction is replayed; evidence review,
any substantive correction, interpretation and interpretation review make fresh API
calls. Reused extractions are explicitly recorded and excluded from `api_calls`;
run attempt-budget counters include the replay. This avoids changing the generated
claim set merely to retest corrected attachment/review behavior. It does not change
the production comparator's normal fresh-generation path.

After these corrections the full suite passed **368 tests plus 13 subtests**.

### Eligible-target review contract

Live MOMENTKV validation exposed a separate handoff defect: a comparison reviewer
objected to two missing pairs that the evidence gate had intentionally withheld.
The full immutable target list was present for context, but the reviewer was not
explicitly told which targets it was allowed to review. `comparison.py` now sends
`eligible_target_ids` and `withheld_target_ids` to the interpretation reviewer.
`comparison_prompts.py` (review version
`novelty_comparison_review_v6_3_eligible_scope`) makes clear that withheld targets
are not omitted comparisons and must not be generated or treated as novel. This
preserves the existing partial-publication contract rather than weakening the gate.
A regression verifies exact agreement between eligible IDs and supplied pairs.

For targeted validation, the live comparison tool additionally supports
`--reuse-evidence-from DIR`: only evidence-stage responses whose **entire structured
request** matches the new request are replayed; otherwise the API is called normally.
Interpretation and its review always remain fresh. Replayed responses and their
paths are recorded separately from API calls. This was used to isolate the handoff
fix without rerolling the already reviewed scientific evidence.

The final full suite after this handoff fix passed **369 tests plus 13 subtests**,
with the same existing FAISS/NumPy warning. Earlier counts above describe earlier
checkpoints, not additional independent tests to sum.

The first successful partial handoff also exposed an evidence-view export bug:
`PaperComparison.evidence_view()` passed all requested targets to a renderer that
requires exact pair coverage. It now passes only the accepted targets to that
renderer; `outcomes()` continues to report every target, including withheld ones.
The partial-publication regression checks both counts. The already saved MOMENTKV
live result was reloaded and its failed evidence-view export/summary regenerated
locally after this fix, without making additional API calls or changing its result.

### Final live results for the result-scope follow-up

All live calls finished. This follow-up used **92 additional DeepSeek API calls**;
saved-response replays are excluded. The complete per-run ledger (including failed
attempts and controls) is
`/home/hema/research_runs/novelty_result_scope_validation_20261004.json`.
This is separate from the earlier 85-call evidence-first validation.

| Run directory under `/home/hema/research_runs/` | New API calls | Outcome |
|---|---:|---|
| `novelty_scope_controls_live` | 16 | 6/9 matched; ambiguous model scope accepted, and two cases exhausted review repair. |
| `novelty_scope_controls_refined` | 10 | 2/6 matched; schema and citation-binding failures retained. |
| `novelty_scope_caption_refined` | 3 | 1/2 matched. |
| `novelty_scope_controls_final` | 11 | 6/9 matched; model-qualification and material/incidental-condition controls matched, isolated caption controls remained inconsistent. |
| `novelty_scope_context_restored_live` | 3 | 0/2 matched; exposed the caption/baseline association bug and additional citation requests. This run predates the corrected association rule. |
| `novelty_comparison_v5_scope_live` | 10 | CriticalKV complete with 5 accepted pairs after citation repair; MOMENTKV exhausted review-format repair. |
| `novelty_comparison_v5_moment_scope_retest` | 2 | MOMENTKV failed extraction repair (malformed JSON). |
| `novelty_comparison_v5_scope_final` | 16 | CriticalKV complete; MOMENTKV unresolved. Predates the final caption mapping and eligible-target review contract. |
| `novelty_comparison_v5_scope_repaired` | 17 | Two extractions replayed; original citation fixes present, but interpretation reviewers objected to intentionally withheld pairs. |
| `novelty_comparison_v5_scope_handoff` | 2 | Four evidence calls replayed exactly; MOMENTKV 3 accepted / 2 withheld. Partial evidence export recovered locally after renderer fix. |
| `novelty_comparison_v5_critical_handoff` | 2 | Three evidence calls replayed exactly; 5 automatically accepted, but manual inspection found a missed citation defect. This replay is not accepted for downstream use. |
| **Total** | **92** | No clean all-cases live acceptance claim. |

The final standalone controls caught the original ambiguous mixed-model claim and
an explicit false attribution of the numerical margins to both models, while accepting
the correctly qualified claim. They also recognized the same investigated relationship
on a different model, and rejected generalizing a stationary-workload result to a
changing-workload claim. The two deliberately unattached-caption controls were sometimes
accepted by the reviewer when production attachment was bypassed. The positive CriticalKV
caption case requested extra supporting references and consequently did not match its
expected immediate acceptance. These are recorded failures, not reclassified as passes.
The deterministic attachment/record invariant addresses the supported caption layouts;
it does not make the semantic reviewer infallible.

Source inspection confirms the original defects are corrected in inspected records:

- MOMENTKV C9 explicitly scopes the +1.35 and +0.59 margins to LLaMA-3.1-8B.
- MOMENTKV C11 carries Table 5's LongBench/LLaMA/L=128 caption and the SnapKV baseline
  block along with its table values. C13 also gains the benchmark-naming source.
- The MOMENTKV sigma claim explicitly notes that the cited measurement passage does
  not further specify which model/benchmark produced those values; it does not invent
  that qualification from the overall evaluation setup.
- CriticalKV's Ruler/40%-cache caption is attached. In the earlier complete corrected
  run (`novelty_comparison_v5_scope_live`), the alpha-setting clause was split into
  separately cited clm-15; it even retains the source's Algorithm 1 discrepancy
  (input alpha = 0.25 versus prose alpha = 0.5). The corrected CriticalKV paper object
  still validates against the current schema.

MOMENTKV's latest handoff accepts H1, H3 and H4. Direction and H2 remain withheld:
the second evidence audit used an additional formula passage for C2
(`436775470aa1#p7:42610cf2750b`) that was not attached after the one evidence revision.
The gate keeps that remaining citation gap explicit; it does not turn withholding
into a claim of novelty. The final interpretation reviewer now respects the eligible
set and no longer rejects the supported pairs merely because these two are absent.

**A remaining semantic miss must be explicit.** The latest CriticalKV handoff replay
started with the earlier flawed extraction. Its reviewers accepted clm-06's alpha = 0.5
clause as supported by assumption/theorem passages that do not state that experimental
value. It is supported elsewhere, at `p5:b3b3103572ca` / `p5:0aa0cd425866`, and had been
corrected in the earlier run, but review consistency is not established. A separate
`manual_findings.json` in `novelty_comparison_v5_critical_handoff/` flags that replay
`accepted_for_downstream: false` without rewriting the raw model output. Do not select
it merely because it is the latest automated pass. This shows the limit of the fix:
explicit checks and deterministic caption binding improve traceability and catch the
specified errors, but a reviewer can still misread which passage supports a clause.

Implementation and regression validation are complete; live semantic acceptance is
**mixed**, not fully passed. No literature-wide novelty result is produced. Changes
remain uncommitted. Future aggregation must not treat the withheld MOMENTKV targets
or the manually rejected CriticalKV replay as validated novelty evidence.

### 2026-10-04 — Meaning of the remaining issues for sections 13–15

The implementation and preceding validation documentation were committed in
`bbd4ad0`; references above to uncommitted changes describe the pre-commit state.
The live calls did execute. Their mixed results concern evidence completeness and
review consistency, not inability to access the API or run the pipeline.

These observed failures do not establish that every eventual novelty judgment will
be wrong or that the hypotheses are scientifically weak. For the specific remaining
citation defects, inspection has not demonstrated that correcting them changes the
scientific-overlap conclusion. This is not proof that they are harmless in every case:
missing a material experimental condition or relevant prior result can change that
conclusion and produce a false novelty judgment.

Keep three assessments separate:

- **Evidence accuracy:** whether a statement about prior work is supported, properly
  qualified and correctly referenced (section 12).
- **Novelty:** whether prior work already pursued the direction or investigated the
  scientific relationship, assessed across retrieved comparisons (sections 13–14).
- **Scientific quality:** whether the gap and mechanism are meaningful and the
  hypotheses are testable and falsifiable (section 15). Novelty alone does not make
  a hypothesis valuable; the research critic is not a substitute for source grounding.

The remaining issues do not require halting implementation of sections 13–14.
Proceed with supported comparisons while retaining unresolved, skipped and manually
rejected evidence explicitly. If an unresolved comparison could change a novelty
judgment, that judgment must remain provisional/unresolved until the evidence is
revisited. Withholding, missing citations, failed review or lack of a retrieved match
must never count as positive evidence of novelty. Do not silently drop such papers
from an aggregate and then claim no relevant prior work exists.

Downstream output should distinguish supported novelty within the searched corpus,
overlap found, and novelty unresolved, alongside the architecture's richer overlap
and refinement outcomes. These are planned uncertainty-handling requirements, not
implemented novelty verdicts or a guarantee of literature-wide novelty. Neither a
different model/dataset alone nor a target number of output hypotheses should cause
an unresolved candidate to be promoted to novel. Section-12 reliability work can
continue alongside aggregation implementation; it remains open rather than being
reclassified as fully validated.

### 2026-10-04 — Sections 13–14: joint novelty assessment and refinement

The earlier sections-13/14 requirements above are now implemented. This addition
consumes section 12's comparisons; it does not change Landscape Builder,
Cross-Paper Reasoner, Opportunity Miner, Direction Generator, retrieval, or the
section-12 evidence extraction/comparison algorithm. Section 15's scientific
critic remains a separate, unimplemented stage.

| Module | Added or changed behavior |
|---|---|
| `novelty/assessment_schemas.py` (new) | Typed assessment inputs/results, artifact-bound invalidations, target coverage, evidence references, refinement edits, independent scope checks, and persisted review binding. |
| `novelty/assessment_evidence.py` (new) | Verifies cross-stage lineage, builds coverage and the accepted-evidence packet, resolves invalidation dependencies, and checks generated references/edits. |
| `novelty/assessment.py` (new) | Joint synthesis per direction, fresh independent review, at most one substantive correction, bounded format repair, concurrent independent directions. |
| `novelty/assessment_prompts.py` (new) | Scientific-overlap reasoning, preservation of result scope, uncertainty-aware review, and refinement rules. |
| `novelty/assess.py` (new) | CLI for saved generation, search, comparisons, and optional explicit invalidations. |
| `novelty/pipeline.py` (changed) | Routes assessment reviews and their format repairs to the separately injected reviewer/model. Existing signature/comparison routing is preserved. |
| `tests/test_novelty_assessment.py` (new) | Coverage, provenance, failure, refinement, review, and concurrency regressions. |
| `tools/validate_novelty_assessment.py` (new) | Real-artifact live runs with raw requests/responses and summaries. |
| `tools/validate_novelty_assessment_controls.py` (new) | Predeclared synthetic live scientific-overlap controls; these exercise synthesis/review, not extraction or retrieval. |

**Inputs and trust boundary.** `NoveltyAssessor.run(generation, search,
comparisons, invalidations=...)` strictly reloads the existing typed artifacts.
Generation/search digests, each candidate snapshot, signatures, complete target
identities, shortlists, retrieval statuses and prior-paper artifact hashes must
agree before any API call. One comparison artifact is authoritative per run:
the stage does not silently merge older passing comparisons with newer failures.
The result embeds the input snapshots and their digest so saved assessments can
be revalidated without reopening an index or loading changed paper files.

The model sees the original proposal, reviewed signatures, every target's
coverage, all accepted comparisons/relationships/claims together, and the complete
available extracted passages of papers with accepted pairs. Passages are
deduplicated within a direction. Unsupported/withheld comparisons remain visible
in the coverage ledger but cannot be cited as accepted evidence. No arbitrary
batch partitions hypotheses or related findings within a direction. Independent
directions run concurrently; author → review → correction → re-review is
sequential because those steps depend on their preceding outputs.

**Coverage and invalidation.** Each original direction/hypothesis retains every
shortlisted paper as `reviewed`, `withheld`, `skipped`, `failed`, or
`invalidated`. Missing referenced extracted pages, incomplete retrieval,
upstream diagnostics and an empty shortlist all block complete coverage. This
does not infer PDF completeness or require appendices.

Manual findings must be supplied explicitly as `novelty_invalidations_v1`.
The manifest contains `comparison_sha256` (the existing canonical
`digest(comparisons.model_dump())`) and entries with `direction_id`,
`paper_id`, optional `target_ids`/`claim_ids`, and a reason. Empty target and
claim lists invalidate the whole paper for that direction. Claim-scoped entries
invalidate accepted pairs depending on those claims through relationships or
comparison references. Unknown references and mismatched digests fail before
calls. Arbitrary nearby `manual_findings.json` files are not discovered
automatically: callers must supply known invalidations. For the documented
CriticalKV replay, the validation explicitly converts the manual rejection into
a hash-bound whole-paper invalidation.

**Assessment.** Each target receives a separate finding:
`ALREADY_STUDIED`, `PARTIAL_OVERLAP`, `COMPONENTS_KNOWN`,
`LOW_PRIOR_OVERLAP`, or `UNRESOLVED`, with claim references and a scientific
distinction where justified. This is synthesis of relationships and conditions,
not majority voting over paper labels. A supported matching paper can establish
overlap even when other comparisons are unfinished. A direct empirical test with
a negative/mixed/inconclusive result still investigates that relationship;
theoretical analysis is distinguished in reasoning, and inference/discussion
cannot be called an empirical test. Components studied separately do not prove
their interaction was studied; a new model name alone does not establish a new
scientific relationship.

Code permits low prior overlap only with complete recorded retrieval/comparison
coverage, accepted same-target evidence, a stated scientific distinction, and no
accepted same/very-close/direct/inferred counterevidence. Coverage gaps never
become positive novelty evidence. `CandidateAssessment.outcomes()` separates
the finding from the novelty status: partial overlap with incomplete coverage
still has `novelty_status=unresolved`. All results retain
`literature_wide_novelty=unverified` and `scientific_quality=not_assessed`.
The strongest low-overlap conclusion is scoped to the saved retrieval and
available extracted evidence; it is not a guarantee about all literature.

**Independent review and repair.** The reviewer receives the same evidence and
the draft in a fresh conversation, without the author's correction history.
Every target needs a scope check: a result's unspecified model/benchmark cannot
be filled from a broad evaluation setup, and an inferred comparator cannot become
an observed one. Acknowledged synthesis-scope defects require revision even if
the overlap label would remain unchanged. Correctly reporting incomplete coverage
can pass review; an unresolved novelty question does not itself mean the
synthesis is incorrect.

There is at most one substantive correction and one format/reference repair per
author/reviewer call. Invalid responses, provider failures, budget exhaustion,
unresolved review and missing accepted evidence withhold the assessment and
retain diagnostics. A reviewer can request targeted section-12 reassessment.
Such requests **always withhold publication**, even if the reviewer considers the
draft's uncertainty handling correct and returns `pass`. They are retained as
actionable requests; this stage never silently repairs upstream evidence.
Separate reviewer functions/models are supported. The live checks here use fresh
DeepSeek conversations with the same model, not a different-provider ensemble.

**Refinement and downstream contract.** The original proposal is immutable.
Refinement is an explicit `retain`, `narrow`, `reframe`, `reject`, or
`defer` plan. Retained/removed IDs partition the original hypotheses; novelty-based
removal requires positive already-studied evidence. A known H1 can be removed
while H2 survives unchanged. Scientific edits identify existing proposal fields
and their new values; they cannot overwrite IDs, evidence or grounding rationale.
This first version proposes edits to existing hypotheses, not new hypotheses
or a silently applied replacement candidate.

`refinement_handoff(direction)` reports unchanged experiment-to-surviving-
hypothesis links, unresolved targets, original candidate hash, and targets
requiring a fresh novelty check. Removing a hypothesis changes direction scope;
scientific edits require renewed generation consistency checks followed by
signature/search/comparison for changed targets. Global scientific edits affect
all retained hypotheses. Rejected candidates stop. An unchanged candidate with
unresolved coverage resolves that coverage; eligible unchanged candidates can
proceed to the later scientific critic. There is no automatic scientific-edit
application or execution of later stages in this module.

**CLI and budgets.**

```bash
uv run python -m novelty.assess \
  --directions directions.json --search novelty_search.json \
  --comparisons comparisons.json --invalidations invalidations.json \
  --out novelty_assessment.json
```

Omit `--invalidations` only when there are no known manual exclusions to supply.
The CLI refuses to overwrite output and writes a result even when synthesis is
withheld. Exit 0 means every candidate has an independently accepted assessment;
it does **not** mean novelty was established. The JSON summary includes outcomes
and refinement handoffs. Input/output allowances remain 500,000 tokens by
default (actual provider limits still apply); a complete prompt over allowance
fails visibly rather than dropping papers. All generation, review, correction
and format-repair attempts share the existing call budget and semaphore.

**Validation results (this implementation).** The full repository suite passed:
**447 tests plus 13 subtests**, including 78 assessment tests. The only warning
is the pre-existing FAISS/NumPy deprecation. Checks cover saved-result tampering,
cross-stage mismatch, whole-paper and claim/target invalidation, real skipped/
failed/unresolved input states, missing referenced pages, empty shortlists,
partial retrieval, absent signatures, negative empirical results, known work
despite other gaps, separate components, changed-field rechecks, surviving test
links, independent reviewer routing, bounded repairs, provider/budget failures,
and shared-semaphore concurrency. The CLI help and patch-format check pass.

Completed **36 DeepSeek calls** for this stage, with raw calls preserved:

| Artifact under `/home/hema/research_runs/` | Calls | Result |
|---|---:|---|
| `novelty_assessment_moment_live/` | 3 | Initial review returned pass with issues, then incorrectly treated incomplete novelty coverage as grounds to abstain. No assessment published. |
| `novelty_assessment_moment_v2_live/` | 2 | Automated pass, manually rejected: synthesis attached general evaluation models to a measurement whose model/benchmark was unspecified. Reviewer had called this non-blocking. |
| `novelty_assessment_controls_live/` | 13 | 5/6 expectations matched. The source-contradiction case was safely withheld but lost its typed reassessment request because pass-plus-request failed validation twice. |
| `novelty_assessment_moment_v3_live/` | 2 | Automated pass, manually rejected: H1 prose blurred the observed sigma range with a theoretical validity boundary. |
| `novelty_assessment_controls_v3_live/` | 13 | All six predeclared expectations matched; source contradiction produced an explicit upstream reassessment request and no published assessment. |
| `novelty_assessment_range_scope_control/` | 1 | Reviewer rejected the deliberately false theoretical-cutoff assertion. Exploratory probe: its reused candidate was also off-topic, so this is not an isolated or clean paired semantic evaluation. |
| `novelty_assessment_moment_v4_live/` | 2 | Final accepted synthesis, independently reviewed and inspected against cited claims/passages. |
| `novelty_assessment_critical_invalidated/` | 0 | Exact CriticalKV manual rejection blocked all five targets; no API call and no novelty promotion. |

The six final live control expectations were: a negative test still counts as
already studied; that positive overlap survives skipped other work; a model
change alone does not make the relationship new; separately known components
do not establish their interaction; a mechanism-relevant regime change leaves
a possible distinction; and contradictory source evidence routes back to
section 12. These are small synthetic boundary controls, not a measured rate of
novel or scientifically useful hypotheses.

The final review contract separates **correct synthesis** from **sound upstream
evidence**: pass-plus-reassessment is allowed as a report, but publication is
unconditionally blocked by any reassessment request. Unit coverage explicitly
verifies that boundary. Per-target scope checks additionally block acknowledged
qualifier defects; final prompts distinguish an observed range from the domain
of a theoretical claim. Earlier unsuccessful runs remain recorded, and the v2/v3
MOMENTKV trials have `manual_findings.json` marking them unsuitable for downstream
use. Only the final v4 run above is the inspected real-assessment example.
These prototype runs are validation history, not alternate versions to select
merely because they show an automated pass.

Final MOMENTKV assessment:
- Direction and H2: **UNRESOLVED**, because their section-12 comparisons are withheld.
- H1, H3 and H4: **PARTIAL_OVERLAP**, preserving empirical versus theoretical
  distinctions and the unspecified model/benchmark of the sigma measurements.
- All five targets: remaining novelty **unresolved**, because the saved run
  skipped other shortlisted papers.
- Refinement: **defer**, retaining all four original hypotheses and their test
  links unchanged. The next action for this artifact is to resolve coverage.

Inspection confirms the final output leaves the sigma-measurement models/
benchmarks explicitly unspecified, preserves the numerical result qualifiers
where repeated, and treats the measured band as an observed range rather than
claiming theory becomes invalid above 1.8. The saved final assessment reloads
against the final schema, as does the zero-call invalidation artifact.

**Remaining limits.** This completes implementation and the listed validation
of sections 13–14; it does not resolve the earlier section-12 missing citations,
compare the complete real shortlists, or establish literature-wide novelty.
The real test used one saved direction with partial coverage. Independent model
review remains fallible, as the preserved failed trials demonstrate; the scope
checks enforce recorded defects, not perfect semantic detection. No repeated
stability study, complete-shortlist novelty validation, or hypothesis-usefulness
evaluation was performed. Refinement plans are reviewable outputs; scientific
edits and section-15 criticism still require their separate downstream work.
No implementation changes in this section have been committed yet.

### 2026-10-04 — Full-shortlist execution and 1M input allowance

The live-run requirement for this project is the complete agreed shortlist;
a selected-paper diagnostic must not be substituted for that run. The MOMENTKV
candidate has 100 target–paper pairs across 54 distinct papers (20 shortlisted
papers for each of the direction and four hypotheses). The full run uses fresh
section-12 extraction and reviews for every shortlisted paper, including MOMENTKV
and CriticalKV, rather than reusing the previously rejected replay.

At the user's request, the local `.env` now sets
`NOVELTY_MAX_INPUT_TOKENS=1000000`. Output allowance remains 500000, subject to the
configured provider output maximum. Aggregation retains every accepted comparison
and the complete available source passages; the 54 papers contain 559 extracted
pages (approximately 616343 tokens before comparison/assessment context). No
findings or papers are removed to fit the earlier 500k input allowance.

The already-running per-paper comparisons retain their recorded 500k input
setting because each individual request fits it; new assessment/recovery
instances read the 1M override. The assessment regression suite passes all 78
tests with that override. Full-run execution results follow below once complete.

#### Complete section-12 execution and recovery ledger

The full saved MOMENTKV shortlist was executed: **100 target–paper pairs across
54 distinct papers; zero pairs skipped**. This is the saved index shortlist, not
an exhaustive comparison with every paper in the literature. The original run
and both recovery runs have finished and their raw responses remain preserved.

| Artifact under `/home/hema/research_runs/` | Actual API calls | Scope/result |
|---|---:|---|
| `novelty_kv_complete_comparison/` | 310 | All 54 papers; 30 complete, 4 partial, 18 unresolved, 2 failed paper results. |
| `novelty_kv_recovery_round1/` | 22 | Three closed structural failures; latest results: 1 complete, 1 partial, 1 unresolved. |
| `novelty_kv_recovery_round2/` | 40 | Nine closed evidence failures; latest results: 6 complete, 3 unresolved. Six additional local extraction replays are **not** API calls. |
| `novelty_kv_consolidated_v1/` | 0 | Validated latest-attempt consolidation: 37 complete, 5 partial, 12 unresolved papers; 69 accepted pairs, 31 withheld. |

The six replayed extractions only attached the exact existing passage IDs named
by the previous scope audit. Claim text and relationships were unchanged; each
received a **fresh independent evidence audit**, interpretation and review.
Other recoveries supplied the failed attempt/objections to source-grounded
regeneration; independent reviews remained fresh. A recovery replaces the entire
selected paper's prior result even when the recovery still fails. The
consolidator never picks whichever attempt has a favorable outcome. Generation,
search, candidate, signature, shortlist, target and prior-artifact identities are
checked before replacement; per-paper lineage and input digests are saved.

| Original target | Accepted comparisons | Attempted shortlist |
|---|---:|---:|
| Direction | 13 | 20 |
| H1 | 13 | 20 |
| H2 | 16 | 20 |
| H3 | 13 | 20 |
| H4 | 14 | 20 |

Both MOMENTKV and CriticalKV now have five accepted target comparisons in these
fresh runs. This supersedes their earlier incomplete/invalidated diagnostic
artifacts for this full-run handoff; it does not retroactively approve those
older outputs. The other 31 pairs are withheld for remaining claim/scope defects,
citation gaps, or malformed comparison/review responses. Exact latest diagnoses
are in `novelty_kv_consolidated_v1/unresolved_details.json`; being attempted does
not mean evidence was accepted. These gaps must not become a low-overlap finding.

#### Full-context transport and provider limit

The first full assessment attempt, `novelty_kv_full_assessment_v1/`, was rejected
before generation. DeepSeek reported **1,258,954 input tokens**, plus the client's
393,216-token output reservation, exceeding its 1,048,576-token context limit.
This is a provider rejection, not an approval restriction or a novelty result.
The local `chars/4` counter underestimated provider tokens; a local 1M allowance
does not enlarge the provider context window.

The implementation changed as follows, preserving the earlier design and saved
provenance rather than removing papers or batching the scientific judgment:

- **`novelty/pipeline.py` (existing):** compact Unicode-preserving JSON; estimate
  actual message text plus conservative framing overhead instead of counting a
  second JSON encoding's escaped quotes/newlines. Provider limits still apply.
- **`novelty/assessment_evidence.py` (new section-13/14 module, updated):**
  `model_packet()` sends every source passage's exact ID and complete text.
  Repeated page hashes, page IDs and byte offsets stay in immutable saved inputs
  and are checked using the original packet. All accepted comparisons, claims,
  relationships, targets and coverage rows remain in the model request.
- **`novelty/assessment.py` (new module, updated):** author, independent reviewer,
  revision and format-repair calls use that same complete model-facing packet;
  validators and persisted lineage continue using the original input records.
- **`tests/test_novelty_assessment.py`:** added lossless transport/provenance and
  exact input-budget-boundary regressions. The assessment suite now has **80
  passing tests**. The preceding shared-call change also passed the 173-test
  assessment/comparison/evidence/search regression suite.

The second full assessment run uses the unchanged local **1,000,000 input**
allowance and a run-specific **65,536 output** reservation so prompt and response
can fit together. This output setting does not modify other modules' allowances.
All accepted-paper source text and all 100 comparison coverage rows are retained.

#### Full sections-13/14 outcomes and final validation

| Artifact under `/home/hema/research_runs/` | API requests | Outcome |
|---|---:|---|
| `novelty_kv_full_assessment_v1/` | 1 | Provider context rejection described above; no generated assessment. |
| `novelty_kv_full_assessment_v2/` | 4 | Full-context synthesis, review, correction, review. Reviewer caught missing numerical qualifiers but then passed an unsupported link between separate claims. Manually rejected; see `manual_findings.json`. |
| `novelty_kv_full_assessment_v3/` | 2 | Full-context correction with explicit recorded feedback, followed by a fresh independent review: pass; final synthesis inspected against decisive source claims. |
| `novelty_scope_join_control/` | 1 | Fresh full-context review of the exact erroneous v2 draft correctly requested revision for the unsupported measurement-to-ablation linkage. |

The v2 error was specific: H3 called the named order ablation a **low-sigma
operating point**, while the separate sigma-measurement claim leaves its model
and benchmark unspecified. Repeating that caveat elsewhere does not establish
that the ablation used the measured sigma regime. The final v3 text separates
the ablation from those aggregate measurements and explicitly leaves its spread
unknown. Existing **`novelty/assessment_prompts.py`** now uses v5 author/review
instructions prohibiting unsupported condition transfers between separate
claims. This is a synthesis correction; original candidates, accepted evidence
records and all comparison coverage remain unchanged. The v3 runner and feedback
manifest preserve the exact rejected draft, feedback and source digest; the
independent reviewer receives no correction history.

The final artifact contains **five published assessments**, rather than zero
section-14 outputs:

| Target | Finding | Remaining scientific distinction in the accepted evidence |
|---|---|---|
| Direction | PARTIAL_OVERLAP | Jointly diagnose moment-approximation error, normalization bias and accuracy in deliberately populated high-spread strata. |
| H1 | PARTIAL_OVERLAP | Test whether MOMENTKV's accuracy advantage changes with measured logit spread at matched settings. |
| H2 | PARTIAL_OVERLAP | Test whether downward-biased Jensen normalization protects accuracy compared with exact/less-biased normalizers at high spread. |
| H3 | PARTIAL_OVERLAP | Test how the first-order versus zeroth-order gain changes across measured spread strata; the existing order ablation does not establish that relationship. |
| H4 | PARTIAL_OVERLAP | Test whether moment-informed eviction's suppression of spread saturates in high-spread strata. |

Refinement is **defer**, preserving all four hypotheses and their test links,
with no scientific edits or removals. The supported distinction is a question
about the behavior of an existing method, not a claim that its components are
new. These are perceived overlap/distinction outcomes. Remaining novelty is
**unresolved for all five targets** because the full attempted run still has 31
withheld comparisons. No target is promoted to LOW_PRIOR_OVERLAP, and no numerical
novelty probability or guarantee is inferred from acceptance counts.

The final saved artifact reloads through the strict schema. Verification checks
that all 100 coverage rows survive, no paper is skipped, and every comparison and
complete available accepted-paper source text is identical in the author and
reviewer requests. Original offsets/hashes remain preserved in saved inputs.
**174 novelty regression tests pass**, including 80 assessment tests; the final
source-scope control correctly rejects the known erroneous draft. The final
assessment has a fresh passing independent review with no reassessment requests.
Manual inspection confirms the known sigma/ablation linkage is removed and the
observed sub-1.8 range is not made a theoretical validity boundary. This inspection
is focused on synthesis and decisive claims, not a ground-truth audit of every
sentence across all 54 papers.

Full-run totals: **380 API requests: 379 successful responses and one context-limit
rejection**, plus six separately accounted local extraction replays. All calls
have finished. The complete ledger is
`/home/hema/research_runs/novelty_kv_full_run_summary.json`; authoritative results
are `novelty_kv_consolidated_v1/result.json` and
`novelty_kv_full_assessment_v3/result.json` under the same directory. All earlier
failed/rejected artifacts remain preserved. The saved assessment routes the next
work to **resolve_coverage**; section 15's scientific critic has not run. This full
execution supersedes the earlier statement that only a selected-paper assessment
had been run, but does not erase the remaining evidence failures or establish
literature-wide novelty. Current implementation/documentation changes remain
uncommitted.

### Targeted section-12 repair — implementation follow-up

The full-run failures exposed a repair-loop problem in the implementation above:
a local citation or claim objection previously regenerated the entire evidence
record, and a pair-rationale grounding objection automatically reopened evidence.
That could introduce unrelated defects. The single evidence-correction allowance
also left newly identified, actionable defects stranded after a fresh audit.

The implementation now changes these existing modules and adds one internal helper:

- **`novelty/comparison_repair.py` (new):** typed sparse `EvidencePatch` and
  `InterpretationPatch` responses, explicit edit scopes, validated attachment of
  reviewer-named source IDs, and exact reviewer field-address maps. Code merges
  allowed replacements by stable claim/target ID. Unaffected claims and target
  comparisons are preserved; duplicate, unknown, and out-of-scope edits fail.
  Evidence additions require an explicit omission/reopen. A citation attachment
  does not approve its supporting claim.
- **`novelty/comparison_workflow.py` (changed):** known missing citations are
  attached before the next audit. Pure citation defects do not require a model
  to rewrite claim text. Scientific objections use sparse claim/mapping patches;
  all resulting claims and relationships receive a fresh independent audit,
  including an omitted-evidence check over the original passages. Subsequent
  newly discovered defects can receive up to three corrections per evidence
  phase. An identical/cycling record gets one fresh disagreement audit, then
  stops; the shared API attempt budget remains enforced. Outcome diagnostics
  distinguish processing errors, citation/claim repair, coverage review, and
  model abstention from an accepted insufficient-evidence finding.
- **`novelty/comparison.py` (changed):** objections to a pair's rationale/reference
  now patch that target's interpretation, preserving the evidence and other
  target pairs. Objections to evidence claims, or explicit requests for omitted
  evidence, reopen evidence. Independent interpretation reviewers receive exact
  valid field paths with target/dimension labels. Format repairs remain separate
  bounded calls; failed local interpretation repairs retain the last valid review
  and its accepted unaffected pairs. Independent papers remain concurrent.
- **`novelty/comparison_records.py` and `comparison_schemas.py` (changed):**
  comparison v6 permits at most eight consecutive evidence-review versions:
  initial audit plus three corrections, and at most one comparison-triggered
  reopen plus three corrections. Existing v5 artifacts remain readable without
  changing their serialized schema version or hashes; v5 cannot claim the expanded
  history. Every interpretation remains bound to its exact evidence version, and
  the published result must match the latest independent accepted projection.
- **`novelty/comparison_prompts.py` (changed):** explicit sparse-edit instructions,
  original-source verification, preservation of counterevidence, and reviewer
  address selection. These replace broad regeneration for local defects; they do
  not lower evidence-acceptance requirements.
- **Tests (changed/new):** existing comparison, relationship and scope fixtures
  now exercise sparse responses. `tests/test_novelty_targeted_repair.py` adds
  adverse-edit rejection, unaffected-content preservation, dependency rechecking,
  citation repair without automatic acceptance, pair-only grounding repairs, and
  retention of valid state after a failed patch. The complete novelty suite
  passes **219 tests** after the bounded-repair change.

The live follow-up resumes all 17 affected papers from their exact saved records.
Only structurally valid, identical-context earlier audits can be replayed. All
new edits are reviewed through fresh API calls; replayed checkpoint responses are
accounted separately. Candidate, source, signature and shortlist identities remain
unchanged. Final coverage and assessment results are recorded below after the
follow-up completes.

The larger accepted comparison set also required a lossless transport update in
**`novelty/assessment_evidence.py`**: complete source passages are now grouped by
original page ID and use short `srcN` reference labels in the model request.
Every scientific text and accepted comparison is retained. Only typed source-ID
fields are translated; prose is unchanged. **`novelty/assessment.py`** restores
response references to the original IDs before validation or persistence, and
records transport version `page_grouped_source_aliases_v1`. Unknown or wrong-paper
references still fail. **`tools/validate_novelty_assessment.py`** saves the exact
alias mapping alongside the immutable inputs for auditability. Two regressions
check exact restoration of reassessment requests and rejection of unknown aliases.
The complete novelty suite now passes **223 tests**, including successive monotonic
citation repair and stopping after an unchanged disagreement audit.

A final malformed evidence audit exposed an omitted nonempty scope check despite
otherwise complete JSON. **`EvidenceBuilder.audit()`** now supplies the exact IDs
of all result/evaluation/theoretical claims requiring those checks. This supplements
the schema and prompt; it does not invent a check or accept a malformed judgment.

#### Targeted-repair live results — complete shortlist now accepted

This follow-up resolves the **31 withheld comparisons** recorded above. The same
original candidate, signatures, retrieval and 100-pair shortlist are retained;
no paper was dropped and no acceptance gate was relaxed.

| Artifact under `/home/hema/research_runs/` | API calls | Local checkpoint replays | Consolidated accepted pairs |
|---|---:|---:|---:|
| `novelty_kv_targeted_repair_live/` | 52 | 17 extractions + 16 validated audits | 83 / 100 |
| `novelty_kv_bounded_repair_live/` | 32 | 7 extractions + 6 validated audits | 97 / 100 |
| `novelty_kv_final_audit_live/` | 3 | 1 extraction | **100 / 100** |

The first pass tested sparse patches with the previous single-correction bound.
Newly identified source defects motivated the bounded follow-up implementation
above. Six remaining papers completed there; the last paper needed a fresh audit
because its earlier reviewer omitted required result-scope checks. The last run
reused its unchanged evidence record rather than regenerating claims. Exact input
matching remains required for audit replay; otherwise a fresh review is made.
All failed attempts remain saved and replacement is by latest selected paper
result, including failures—not by the most favorable past outcome.

The authoritative **`novelty_kv_consolidated_v4/result.json`** is comparison v6,
with **54 complete paper results, 100 accepted pairs, zero skipped and zero
withheld pairs**. Each target has 20 accepted comparisons and a complete recorded
coverage ledger. `novelty_targeted_repair_integrity.json` additionally verifies
all five live semantic evidence patches: unchanged claims were preserved exactly,
and no sparse patch attempted an out-of-scope edit. Other evidence corrections
were citation attachments followed by independent reviews.

| Assessment artifact | API calls | Result |
|---|---:|---|
| `novelty_kv_complete_assessment_v4/` | 2 | Full-coverage synthesis passed automated review but repeated the unsupported sigma-to-ablation linkage. Manually rejected; `manual_findings.json` records it. |
| `novelty_kv_complete_assessment_v5/` | 2 | Explicit source-based synthesis correction using the same complete packet; fresh independent review passed, followed by focused source inspection. |

**The reviewer limitation remains real:** the prior saved-error control passed,
yet a later full-context review missed the same kind of scope transfer. The fix
to comparison repair does not make model judgment infallible. The erroneous
assessment remains unsuitable for downstream use. Final v5 separates the named
order ablation from sigma measurements whose model/benchmark is unspecified and
does not treat the measured sub-1.8 range as a theoretical validity cutoff.
The final artifact has no upstream reassessment requests and reloads against the
strict schema. Raw author/reviewer packets were checked to contain the same full
scientific context, with exact reversible source labels. This final assessment
used the local 1M estimated input allowance and a **32,768-token output reserve**;
all responses finished normally without truncation. Source text was not sampled.

The final **section-14 result is retain**, replacing the earlier **defer**:

- Direction and H1–H4 all have `finding=PARTIAL_OVERLAP` and complete coverage.
- Their code-owned novelty status is now `partial_overlap`, **not unresolved**.
- The retained distinction is the high-spread diagnostic and its specific
  margin, normalizer, approximation-order, and suppression-saturation questions
  concerning existing MOMENTKV mechanisms.
- All four hypotheses and their original test links are retained unchanged;
  no hypothesis is removed and no scientific edit is silently applied.
- `unresolved_target_ids=[]`; the saved handoff is **`research_critic`**, matching
  architecture **section 15**. This stage has not been implemented/run here.

This is a scoped perceived-novelty/refinement outcome over the recorded retrieval,
not a percentage guarantee of globally novel or scientifically valuable ideas.
The comparison backlog is resolved for this candidate; section 15 evaluates the
quality and significance of the surviving scientific questions.

Follow-up total: **91 successful API calls**, with **47 local checkpoint replays**
accounted separately. All calls have finished. **223 novelty tests pass**. Final
machine-readable ledger: `/home/hema/research_runs/novelty_targeted_repair_summary.json`.
Final handoff: `/home/hema/research_runs/novelty_kv_complete_assessment_v5/result.json`.
Implementation and documentation changes remain uncommitted.

### 2026-10-05 — Novelty applicability after a reviewed direction revision

This extends the sections 9–14 implementation above. New
`novelty/revalidation.py` preserves explicit provenance across candidate versions.
Old final novelty verdicts are never copied to a changed candidate.

Only changes to rationale `statement` prose or experiment `why_this_test` and
`informative_outcomes` are eligible for reviewed reuse; rationale evidence/page
references remain fixed. Corrected source attribution must not leave stale
signature premises or comparisons. All other scientific fields must remain exactly equal. Even eligible
changes require an author assessment and a fresh independent applicability review
covering the direction and every hypothesis, with the full prior evidence and
original/new proposals. An unchanged hypothesis string alone is insufficient;
changed interpretation can change scope. Any changed scientific field, uncertainty,
disagreement or failed review requires fresh novelty search and comparison.

A reuse certificate records exact parent/new generation, search, comparison and
candidate hashes, per-target decisions and their independent review. Rebinding
retains exact signatures, retrieval records and accepted comparisons. It updates
candidate/version references only after validating the certificate, then reruns
joint novelty synthesis and its review. Selective refresh runs signature creation,
review, hybrid retrieval and comparison for affected directions while preserving
unaffected directions. New retrieved artifacts and their lineage are stored too.
Outstanding upstream evidence invalidations cannot be discarded by this path.

`novelty/pipeline.py` now routes applicability and candidate revision reviews to the
independent reviewer. The loop shares one call allowance/concurrency limit across
stages; internal stages borrow the client without closing it. Corpus-wide novelty
remains bounded by retrieved coverage, as before. Live validation results for this
cross-module loop are recorded in [Research Critic](research_critic.md).

The full refinement validation also changed the wire representation in
`novelty/assessment_evidence.py::model_packet`: each original page now maps its
reversible `srcN` keys directly to exact passage text, instead of repeating the
`passage_id` and `text` field names for every passage. All text, page grouping and
source aliases are preserved. This frees response headroom for source-complete
calls; it does not summarize or truncate the papers. Assessment and critic run
metadata identify the new transport version. Saved scientific packets retain
their original structure and hashes.

The full live refinement case revealed an ownership ambiguity in review feedback:
accurate c15/c16 claims were joined incorrectly in an accepted H3 relationship and
comparison rationale. `novelty/assessment_prompts.py` now explicitly routes wrong
upstream interpretations to section 12 even when individual claims are accurate,
and reserves synthesis edits for fields actually owned by the assessment. The
latest live audit still missed this upstream error while finding other synthesis
qualifier omissions; this is a remaining semantic-review limitation, not a claim
that prompt changes guarantee detection. Exact results are in the critic module
history. No flawed prior comparison was silently relabeled as corrected.
