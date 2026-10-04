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
