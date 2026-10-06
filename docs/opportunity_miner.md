# Opportunity Miner

Implemented on 2026-10-03 against [architecture section 6](research_path_generator_architecture_refined.md#6-opportunity-miner).
This stage asks what appears unresolved given reviewed cross-paper findings.
It reuses the existing Landscape, CrossPaperReasoning, and PaperStore contracts;
landscape construction and cross-paper reasoning algorithms are unchanged.

## Flow and contract

1. Validate the canonical `landscape_builder_v1` input and the reasoning output's
   exact landscape hash and topic. Accepted findings, observations, and tensions
   are eligible sources; rejected diagnostics and unreviewed landscape statements
   cannot independently support an opportunity.
2. Give the proposer reviewed sources, the landscape items they refine, and
   relevant concept groups. A candidate contains category, unresolved question,
   rationale, scope, uncertainties, and supplied source/evidence IDs. Categories
   are recurring limitation, restrictive assumption, missing regime, contradiction,
   failure mode, missing evaluation, unresolved tradeoff, and mechanistic interaction.
   An empty proposal is valid.
3. Resolve references in code. Evidence IDs are namespaced by their reviewed
   source; each cited source must own at least one cited evidence record. Reject
   unknown, duplicated, or mismatched references. Preserve extracted
   `author_stated`/`model_inferred` origins from the original JSON pointers.
4. Reload selected `paper.json` artifacts. Check their hashes, source values,
   provenance pointers, and page locations against the cited evidence. Nonempty
   exact value prefixes are allowed because the reasoner may truncate bundles.
   Load all selected evidence pages plus all previously reviewed pages for the
   cited reasoning sources, retaining potentially contrary context. Previously
   reviewed page hashes must match. Missing, changed, unresolved, or over-budget
   evidence produces a diagnostic, never partial acceptance.
5. Review the complete candidate against those original pages. The reviewer
   accepts, rejects, requests correction, or marks it insufficient, checking motivating claims,
   comparability, scope, attribution, and whether reviewed evidence already
   answers the question. Acceptance requires factual accuracy as written, no required corrections, and
   a candidate-relative role assessment for every selected evidence paper. Each
   role must cite that paper's selected evidence and original pages. Code
   constructs the final provenance from loaded artifacts.

The `opportunities_v2` output contains:

- Exact normalized Landscape and CrossPaperReasoning input hashes.
- Accepted opportunities with candidate text, source IDs and landscape `refines`,
  selected evidence paths/hashes/origins, reviewed page paths/hashes, review notes,
  all cited `paper_ids`, separate `supporting_paper_ids` / `context_paper_ids`,
  per-paper role rationales/citations, and explicit `single_paper` or
  `multiple_papers` support based only on the supporting set.
- `verification="supported_in_reviewed_corpus"` and
  `literature_novelty="not_assessed"` on every accepted opportunity.
- Typed diagnostics retaining failed candidates and substantive review vetoes where available, source/batch/
  proposal/acceptance coverage, call attempts, token usage, settings, model names,
  timestamps, and prompt version.

Source attribution records what the extractor classified; it does not prove an
explicit author statement. Page review is still required. An accepted opportunity
is an unresolved question within the reviewed evidence, not a certified novel
research direction. Sections 7 onward generate directions/hypotheses and perform
separate novelty retrieval.

## Factual correction and support roles

Review must report `factual_status`, `required_corrections`, and
`paper_assessments`. A correction, `needs_correction` status, or `revise` verdict
withholds the candidate as `requires_correction`, even alongside `accept`.
Recognized vetoes are checked before schema repair, so repairing malformed
review fields cannot erase a known correction. Uncertain factual support cannot
be accepted. Diagnostics retain the original candidate and veto response; the
miner does not silently rewrite or promote it. A corrected candidate requires
another original-page review.

Each selected paper receives `supporting` or `context_only` relative to the
**new opportunity**. A paper explicitly outside the candidate's scope is context,
not another supporting paper. Upstream evidence stances are preserved but do not
determine this role: two conflicting reports can both support a new question
about their discrepancy. Code validates exact paper coverage, ownership of
selected evidence and reviewed page citations, and nonempty supporting coverage.
All cited evidence and contrary pages remain available in the output.

This changes the output contract to `opportunities_v2`; the prompt version is
`opportunity_v2_review_roles`. Historical v1 files remain historical and are not
silently upgraded into newly reviewed results. Re-run mining on their original
Landscape/reasoning inputs, or extract their candidates for an explicit re-review.
Review identification of errors and roles still involves model judgment; the
code enforces the decisions and references the model explicitly returns.

## Usage

```bash
uv sync
uv run python -m opportunities.miner \
  --landscape landscape.json --reasoning reasoning.json \
  --root /srv/research_finder/markdown --out opportunities.json
uv run pytest tests/test_opportunity_miner.py
```

Use the exact Landscape associated with the reasoning output. Hashes include the
normalized schema defaults; if an old artifact's lineage no longer matches after
a schema change, rerun reasoning on the normalized Landscape rather than editing
hashes. The CLI reads local artifacts and writes only the requested output.
The async library accepts an injected `PaperStore` (including its existing S3
factory) and optionally a caller-owned async chat function. Create one miner and
one store per request. An internally created client is closed when the run ends.

Provider/model: `OPPORTUNITY_PROVIDER` / `OPPORTUNITY_MODEL`, otherwise the general
provider and its configured default model. `OPPORTUNITY_REVIEW_MODEL` optionally
selects a separate review model. CLI `--provider`, `--model`, `--review-model`
override these settings.

| CLI flag | Default | Purpose |
|---|---:|---|
| `--concurrency` | 4 | Maximum simultaneous provider calls |
| `--max-calls` | 40 | Atomic total attempt budget including repairs/failures |
| `--batch-size` | 12 | Reviewed sources per proposal batch |
| `--max-batches` | 8 | Maximum proposal batches; omitted sources are diagnosed |
| `--max-candidates-per-batch` | 5 | Maximum proposed candidates per batch |
| `--max-review-pages` | 20 | Maximum complete page set per candidate |
| `--max-input-tokens` | 40000 | Estimated complete prompt limit, including schema |
| `--max-output-tokens` | 12000 | Proposal output allowance; `OPPORTUNITY_MAX_OUTPUT_TOKENS` |
| `--max-review-output-tokens` | 12000 fallback | `OPPORTUNITY_REVIEW_MAX_OUTPUT_TOKENS`, then `REASON_MAX_REVIEW_OUTPUT_TOKENS` |
| `--repair-rounds` | 1 | Additional attempts for malformed/truncated/schema-invalid output |

The current development environment inherits a 128000 review output allowance
from `REASON_MAX_REVIEW_OUTPUT_TOKENS`. Output allowances are separate from input
limits and do not guarantee that a provider supports the requested amount.
Fatal provider rejections stop queued work; already-running calls may complete.
The CLI saves diagnostics and exits with status 2 for a fatal provider error.
Other partial or empty results may exit successfully; consumers must inspect
coverage and diagnostics.

Independent proposal batches run concurrently. Each ready batch starts its
reviews without waiting for other proposals. A shared semaphore and attempt
counter bound calls; blocking artifact access runs through `asyncio.to_thread`,
with per-resource locks to avoid racing the same cached artifact. Required
proposal → reference resolution → page loading → review dependencies remain.

## Validation

`uv run pytest -q`: **149 passed, 13 subtests passed**. The component tests
cover acceptance, lineage mismatch, unknown references, stale artifacts/pages,
fabricated source values under real hashes, missing pages, single-paper support,
required cross-paper citations, rejection/insufficiency, bounded repairs,
truncation, exact duplicates, empty proposals, page/input/call/batch limits,
fatal provider errors, simultaneous reviews, and reviews overlapping another
batch's proposal, blocking correction signals even in malformed reviews,
uncertain acceptance, context-only support counts, candidate-relative roles,
legacy review rejection, and role evidence/page ownership. The suite reports one existing FAISS/NumPy deprecation warning.

A bounded live run reused the attribution-validation Landscape and its one
accepted cross-paper finding across three papers. It used `deepseek-flash`, a
maximum of eight calls, one batch, three proposed candidates, concurrency four,
and a 128000 review output allowance. Five attempts completed: one proposal,
one repair, and three reviews; no truncation or final diagnostics. Usage was
34152 input tokens and 20190 output tokens (17598 reported reasoning tokens).

| Candidate | Supporting paper | Automated review outcome |
|---|---|---|
| Generalization to larger MoE models and different top-k routing | SemMoE (`432956a1a205`) | Accepted within reviewed model/evaluation scope |
| Compute/communication overlap across multiple nodes | TokenWeave (`b02deed4ad77`) | Accepted with a hardware wording caveat |
| Capacity-routing and multimodal robustness beyond evaluated settings | Capacity-Aware Inference (`9515cc87b003`) | Accepted within reviewed tables/benchmarks |

All three use **single-paper** supporting evidence despite coming from one
cross-paper finding. An independent deterministic audit checked all six selected
evidence records and five unique original review pages against their hashes and
paths, plus both input lineage hashes.

The TokenWeave candidate says “8xH100 and 4xH100 DGX systems”; its reviewer correctly
notes TP-4 uses four GPUs within the same eight-GPU DGX. The candidate was still
accepted as written. This is a concrete limitation of automated scientific
review: retained notes expose the imprecision, but schema and provenance checks
cannot guarantee factual precision in generated prose. Do not interpret three
acceptances as three proven novel or fully human-validated opportunities.

Local artifacts (not checked into Git):

- `/home/hema/research_runs/opportunity_miner_validation/result.json`
- `/home/hema/research_runs/opportunity_miner_validation/provenance_audit.json`
- Inputs: `/home/hema/research_runs/limitation_attribution_validation/landscape.json`
  and `reasoning.json`.

## Current boundaries

Batching follows input source order (findings, tensions, observations); there is
no semantic source clustering or fusion across batches. Deduplication detects
only the same normalized question with the same source set. The live smoke test
covers three missing-evaluation/regime questions, not all eight categories.
No automatic narrowing/redrafting follows substantive review rejection.
Novelty retrieval, ranking, direction/hypothesis generation, and service endpoint
wiring remain separate work. Usage snapshots come from the existing process-wide
tracker; concurrent application requests need request-scoped usage accounting
before those token totals can be treated as isolated per-request measurements.

## Broader validation follow-up

[Broader validation](opportunity_miner.md#broader-validation-history) now records 135 offline tests
plus 13 subtests, 124 live calls, all eight category review controls, eleven
negative probes, and fresh six-paper/two-topic pipelines. It exposes repeatable
acceptance of a factual scope error and a distinction between cited papers and
papers supporting the new opportunity. See that report before interpreting
multiple-paper labels or automated acceptance as scientific validation.

The two exposed failures are addressed by [review and support-role fixes](opportunity_miner.md#review-fixes-history),
which records focused live rechecks without rewriting the historical runs.


## 2026-10-03 implementation update — supersedes batching and allowance defaults above

The earlier sections are retained as the implementation record at that time.
In `opportunities/miner.py`, `Settings.batch_size` changed from 12 to 0:
`OpportunityMiner.run()` now supplies all reviewed findings, observations and
tensions to one proposal call by default. A positive `--batch-size` explicitly
restores partitioning; separate candidate reviews still run concurrently.
An oversized complete prompt produces an `input_budget` diagnostic without
silently splitting or dropping sources.

The input, proposal-output and review-output defaults changed to 500000 tokens.
Input is now configurable with `OPPORTUNITY_MAX_INPUT_TOKENS`; the existing
output variables retain their roles. Explicit settings override environment
values. The active DeepSeek requests are capped at the configured 393216-token
output limit by `llm_client/client.py`; smaller explicit limits stay smaller.
Input and output must also fit the provider's context window. Five candidates,
20 review pages, 40 calls and the existing evidence/review contract remain the
separate selection and execution controls.

The earlier 149-test result and smoke-run counts above are historical. Current
validation passes 159 tests plus 13 subtests; 34 live calls covered paired
boundary controls and three real-paper miner runs, with all evidence audits
passing. See [module implementation changes](opportunity_miner.md#implementation-history) for the
approach, affected functions, configuration and tests, and
[boundary validation](opportunity_miner.md#boundary-validation) for observed outcomes.


<a id="implementation-history"></a>

## Implementation history — Opportunity Miner built as a new module

The `opportunities/` module was first created in this work at `847b910`.
It was not an existing contributor module we merely adapted. It implements
architecture section 6, consuming the existing builder and modified reasoner.
The question is what remains unresolved given reviewed findings; direction and
experiment generation and literature-wide novelty checking belong downstream.

### `847b910` — initial implementation

| New file / component | Implementation and approach |
| --- | --- |
| `opportunities/__init__.py` | New package for the section 6 stage. |
| `opportunities/schemas.py` — `Candidate`, `Proposal`, `Decision`, `Opportunity`, `Diagnostic`, `MiningResult` | Typed proposal/review/output contracts; eight opportunity categories; evidence references and explicit scope/uncertainty; original version `opportunities_v1`. |
| `opportunities/miner.py` — `Settings`, `digest()`, `OpportunityMiner._inputs()` | Require exact landscape/reasoning lineage and reviewed sources; namespace evidence IDs by source; map each source to the builder items it refines. |
| `OpportunityMiner.run()` and proposal prompt | Propose unresolved questions from reviewed findings, observations and tensions with relevant landscape context. Initially partition sources into batches of 12, with up to five candidates each. Validate source/evidence ownership and exact duplicate questions. |
| `OpportunityMiner._review()` and review prompt | Reload artifacts and original pages; verify source values, pointers and hashes; retain previous review pages including counter-context. Check motivating claims, comparability, attribution, scope and whether the pages already answer the question. |
| `OpportunityMiner._call()`, `_json()`, `_resource()` | Async provider semaphore, atomic attempt budget, bounded schema repair, fatal-error handling, threaded file access and resource locks. Independent proposal batches and candidate reviews overlap; dependent steps keep their ordering. |
| `main()` and result assembly | CLI plus async library entry point; save accepted opportunities, diagnostics, exact input hashes, evidence/page provenance, run settings, calls and usage. `literature_novelty` remains `not_assessed`. |
| `tests/test_opportunity_miner.py` | New tests for lineage, invalid references, stale/missing evidence, page/input/call limits, acceptance/rejection, repairs and concurrent execution. |

The miner reuses `reasoning.evidence.PaperStore` and existing landscape/reasoning
schemas. Code resolves provenance; the model chooses questions and evaluates
support. Acceptance is support within reviewed evidence, not a scientific-value
or novelty guarantee. The initial module did not redesign the landscape builder
or the reasoner's scientific task.

### `54082b1` — fix defects found by broader live testing

The initial reviewer accepted a candidate despite factual corrections in its
notes, and the initial support label counted every cited paper even if a paper
only supplied excluding/contextual evidence. These were implementation defects,
not a new discovery-quality evaluation requirement.

- `opportunities/schemas.py`: added `PaperAssessment`, factual status and required
  corrections, separate supporting/context paper IDs, retained raw veto payloads
  in diagnostics, and changed the output version to `opportunities_v2`.
- `opportunities/miner.py`: enforce correction/uncertainty vetoes before a schema
  repair can erase them; review candidates as written; require a role assessment
  for every selected evidence paper with its own evidence/page citations.
  Derive support counts from supporting roles, retaining context evidence.
- `tests/test_opportunity_miner.py`: added regression cases for correction
  signals, malformed-but-vetoed reviews, role partitions, ownership and zero
  support. Added `tests/test_opportunity_quality_controls.py` and the reusable
  `tools/opportunity_validation_cases.py`, `validate_opportunity_controls.py`,
  `validate_opportunity_pipeline.py`, and `validate_opportunity_fixes.py`.

The [full fix record](#review-fixes-history) below preserves observed failures,
32 focused live rechecks and remaining review variability. No automatic rewrite
or extra sequential critique pass was introduced.

### `62de9cd` — all-source context, larger allowances and boundary checks

`Settings.batch_size` changed from 12 to 0 and `run()` now sends all reviewed
sources in one proposal by default. This gives related findings an opportunity
to be considered together rather than pre-separating them by input order.
Positive batch sizes remain explicit opt-in. Complete prompts over the input
allowance produce a diagnostic; the miner does not silently split/drop sources.
Candidate original-page reviews remain concurrent.

Input default changed from fixed 40000 to `OPPORTUNITY_MAX_INPUT_TOKENS`, fallback
500000. Proposal/review output fallbacks changed from 12000 to 500000, retaining
the existing environment override chains. The shared [LLM client](llm_client.md)
separately bounds provider output. Five candidates, 20 pages and 40 call attempts
remain separate limits; this change does not promise exhaustive opportunities.

Tests and tooling added or changed:

- `tests/test_opportunity_miner.py`: 25 sources must all reach one proposal,
  including when `max_batches=1`.
- `tools/opportunity_validation_cases.py`: optional custom case for the existing
  fixture builder, without changing existing category fixtures.
- `tools/validate_opportunity_boundaries.py`: six evidence-boundary controls,
  repeated twice, plus three real miner runs using saved upstream results;
  four concurrent calls, default 48-attempt cap, raw results and audit output.
- Its `audit()` checks saved v2 references, source values, provenance/page hashes
  and support-role ownership; run checks include lineage and full source delivery.
- `tests/test_opportunity_boundary_audit.py`: fixture lineage plus intentional
  broken-role and changed-page cases to check the audit itself.
- `tests/test_token_allowances.py`: miner/reasoner allowance defaults, environment
  and explicit overrides, and shared client request caps.

The [latest live report](#boundary-validation) records 12/12 passing boundary
reviews, 12 accepted out of 15 real candidates, and 18 audited accepted objects
with 63 evidence and 70 page references. The full offline suite passed 159 tests
plus 13 subtests. This is not a maximum-input load test or scientific-value
benchmark. Known limits include model judgment variability and overlapping
accepted questions.

### Reading the historical reports below

Each retained report describes the code and budgets at the time of that run.
The later dated sections supersede earlier defaults and failed behavior; earlier
results have not been rewritten to hide failures. In particular, appendix
extraction is outside current ingestion: unavailable extracted results cannot
establish that experiments were never performed. The earlier suggestion to
follow appendix references is superseded by the clarification in the broader
validation history below.


<a id="broader-validation-history"></a>

## Broader Opportunity Miner validation

Completed 2026-10-03 against implementation commit `847b910`. Production miner
code, prompts, provider settings, and shared corpus artifacts were unchanged.
This report records failures as well as successful checks.

### Offline checks

`uv run pytest -q`: **135 tests passed, 13 subtests passed**. One existing
FAISS/NumPy deprecation warning remains. Nine new tests in
`tests/test_opportunity_quality_controls.py` cover evidence handling for all eight
categories and retention of contrary original-page context even when a candidate
selects evidence from only one source paper.

The offline tests use stub model decisions. They establish evidence handling,
not scientific validity or LLM judgment quality.

### Live setup

The model returned `deepseek-flash`. Two independently bounded suites ran
concurrently using the configured DeepSeek API. Total: **124 calls** out of the
180-call allowance, zero truncated responses, and no recorded provider errors.

| Suite | Calls | Peak concurrent calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| Controlled cases | 42 | 4 | 117517 | 72068 |
| Two fresh corpus pipelines | 82 | 6 | 547736 | 520340 |
| Total | 124 | — | 665253 | 592408 |

Output totals include 521172 reported reasoning tokens. Usage is summed from
isolated process totals, not overlapping component snapshots. Fresh retrieval
used the local lexical index; normalization/aggregation used local embeddings.
Only the configured chat endpoint received model requests.

### Controlled real-model checks

The synthetic documents are explicitly fictional. Their upstream reviewed
observations are hand-authored fixtures, not live Cross-Paper Reasoner results.
This isolates Opportunity Miner proposal and original-page review behavior.

| Category | Hand-authored positive review | Unconstrained generated categories | Accepted generated candidates |
|---|---|---|---:|
| Recurring limitation | Accepted | recurring limitation | 1 |
| Restrictive assumption | Accepted | restrictive assumption | 1 |
| Missing regime | Accepted | missing regime | 2 |
| Contradiction | Accepted | contradiction, missing evaluation | 2 |
| Failure mode | Accepted | recurring limitation, missing evaluation | 2 |
| Missing evaluation | Accepted | missing evaluation | 1 |
| Unresolved tradeoff | Accepted | unresolved tradeoff, missing evaluation | 2 |
| Mechanistic interaction | Accepted | mechanistic interaction, missing evaluation | 2 |

All eight positive review controls passed. The eight unconstrained runs proposed
13 candidates and accepted all 13; 12 cited both papers. They exercised seven
of the eight category labels in generation: the rare-class failure fixture was
classified as recurring limitation/missing evaluation rather than failure mode.
These overlapping categories are not a reliable mutually exclusive taxonomy.
Two proposal repairs removed extra `title` fields; neither involved truncation.

Six synthetic negative controls were all correctly rejected:

- An already-answered median-latency comparison.
- A p99 regression claimed without any tail-latency measurements.
- A false assertion that TP-4 used a separate four-GPU machine.
- A literature-wide absence claim based on two papers.
- A claimed demonstrated joint compression/caching benefit despite no joint test.
- An assertion of identical implementations despite undocumented seed/warmup.

Two additional real-paper probes were correctly rejected: attributing an inferred
SemMoE limitation to an explicit author admission, and claiming its measured
performance improvement remained unanswered.

#### Failed real-paper hardware regression: 3 of 3 repetitions

The saved TokenWeave opportunity's scope says “8xH100 and 4xH100 DGX systems.”
The original page 9 says TP-4 uses four GPUs on the **same eight-GPU DGX**.
All three repetitions still accepted the candidate. One review explicitly called
the misstatement a narrowing note “not grounds for rejection.” Thus correct
rejection of a blatant synthetic hardware error did not transfer reliably to
an incidental factual error embedded in an otherwise plausible real question.

The current prompt tells the reviewer to judge the candidate as written, but
this is insufficient. Before downstream generation, review should require a
correction/review cycle or rejection when factual scope needs correction. There
is currently no structured correction requirement or corrected-candidate re-review.

### Fresh real-corpus pipelines

Both runs freshly retrieved three papers, built a full landscape, selected
representative bounded subsets without changing paper support or item IDs,
ran the actual Cross-Paper Reasoner, and mined opportunities. They were not
reruns of the earlier MoE landscape. Full landscapes and selected subsets are
both saved so the sampling boundary is inspectable.

| Result | KV-cache compression | RAG grounding |
|---|---:|---:|
| Papers | 3 | 3 |
| Full landscape groups / relationships | 147 / 50 | 211 / 81 |
| Reasoning tasks | 8 | 8 |
| Tasks involving multiple papers | 4 | 1 |
| Accepted reasoning findings / observations / tensions | 2 / 10 / 0 | 1 / 7 / 0 |
| Reasoning calls | 16 | 17 |
| Opportunity batches | 4 | 3 |
| Proposed opportunities | 12 | 9 |
| Accepted / rejected opportunities | 9 / 3 | 8 / 1 |
| Miner calls (including repairs) | 18 | 12 |
| Outputs labelled multiple-paper support | 2 | 0 |

KV-cache papers: MOMENTKV (`436775470aa1`), STAR-KV (`fc330d65cf4f`), and
AttentionPack (`712e704db09d`). RAG papers: translative/multi-agent Amharic RAG
(`f8f2ce0be4be`), CF-RAG (`4cb7db179a09`), and TRACE (`95525342129f`).

Examples of useful accepted questions include matched-compression comparisons
for STAR-KV; behavior of MOMENTKV outside measured low-logit-spread settings;
CF-RAG component rankings on benchmarks outside its two-dataset ablation; and
whether TRACE stage-separability findings hold with model-inferred rather than
injected gold trace information.
These are automated acceptances within reviewed evidence, not human-certified
research opportunities.

The four rejected opportunities concerned claims already addressed by original
pages or unsupported absence/attribution: confused AttentionPack/STAR-KV speedup
attribution; supposedly missing STAR-KV context/hardware comparisons; a no-fine-
tuning evaluation supposedly absent based on related work alone; and a RAG
joint-metric evaluation falsely claimed absent despite TRACE's table providing it.

#### Cross-paper synthesis: one shared opportunity, one contextual second paper

KV `op-000-000` concerns generalization beyond the reviewed 7B–13B evaluations
in both AttentionPack and STAR-KV. This is a shared, scoped two-paper opportunity.
A manual spot-check of AttentionPack page 5 and STAR-KV page 7 confirms the
reported evaluation model sets. It remains an inferred coverage question.

KV `op-000-001` is also labelled `multiple_papers`, but explicitly concerns only
AttentionPack's visual/video task coverage and says it does **not** apply to
STAR-KV. Its STAR-KV evidence has stance `contradicts`: it excludes that paper
from the motivating limitation. The code counts unique cited evidence papers,
which conflates a supporting paper with a contextual/contrary paper. Therefore
**two multiple-paper labels do not mean two shared cross-paper gaps**. RAG's
eight accepted opportunities were all single-paper scoped.

The contract should distinguish cited papers from papers supporting the actual
opportunity, with roles reviewed for the new candidate rather than blindly
inherited from the upstream finding. Merely filtering original evidence stances
would not establish their role relative to the newly proposed question.

### Integrity audit and remaining limitations

The deterministic audit passed for all 41 accepted output objects across both
suites (including the three erroneous negative-control acceptances): 117 evidence
references and 114 reviewed-page references. It checked original artifact/page
hashes, value/provenance pointers, source values/locations, limitation origins,
paper-count labels, and exact input lineage for complete mining results.
References are counted per output, not as unique source pages. Passing this audit
does not resolve the semantic review failures.

Several accepted opportunities concern absences in reviewed pages while those
pages explicitly refer to appendices (for example STAR-KV A.5.4/A.1.6). Scope
notes preserve that limitation, but a stronger verifier should follow relevant
appendix references before promoting a missing-evaluation claim. Similar
Amharic evaluation questions and STAR-KV bit-allocation questions also recur;
semantic deduplication across batches remains absent.

This validation supports functioning concurrent end-to-end execution, original-
page rejection, and at least one real shared opportunity. It does **not** justify
claiming reliable scientific review or exhaustive category/topic coverage.
Address factual corrections and support-role classification before treating
accepted opportunities as trusted input to the Direction Generator.

### Reproduction and artifacts

Paid live harnesses (use new output directories):

```bash
uv run python -m tools.validate_opportunity_controls --out /path/to/new/controls
uv run python -m tools.validate_opportunity_pipeline --out /path/to/new/pipelines
```

`tools/opportunity_validation_cases.py` contains the synthetic fixtures.
The harnesses cap attempts at 60 and 120 respectively, preserve failed results,
and keep the shared data read-only. Private local run artifacts:

- `/home/hema/research_runs/opportunity_controls_live/`: fixture inputs, raw
  model requests/responses, decisions, summary, and call timings.
- `/home/hema/research_runs/opportunity_broad_live/`: fresh retrieval inventories,
  full/selected landscapes, reasoning, opportunities, raw responses and timings.
- `/home/hema/research_runs/opportunity_live_audit.json`: integrity audit results.
- `/home/hema/research_runs/audit_opportunity_live.py`: local audit script.

The earlier three-opportunity smoke run is retained separately in
`opportunity_miner.md`; no saved live results were rewritten to hide failures.

### Fix follow-up

The baseline failures above are preserved. [Review and paper-role fixes](opportunity_miner.md#review-fixes-history)
now document v2 correction vetoes, reviewed support/context roles, 149 passing
offline tests plus 13 subtests, and 32 focused live calls validating the fixes.


### 2026-10-03 clarification — supersedes the appendix follow-up above

The earlier report is preserved. Its suggestion to follow appendix references
is not a requirement of the current pipeline: ingestion does not extract
appendices. Missing extracted material must not be treated as proof that an
evaluation was never performed. Withhold that absence claim if the available
pages contradict it or cannot establish it. A page-availability gap alone does
not establish a scientific research gap.

The miner now supplies all reviewed sources together by default, superseding
the explicitly batched configuration used in the historical run above.
Semantic deduplication is still not guaranteed.

[Boundary validation](opportunity_miner.md#boundary-validation) records 12/12 passing
synthetic reviews, three real miner runs accepting 12 of 15 candidates, and
passing saved evidence/role audits across 34 live calls. The offline suite now
passes 159 tests plus 13 subtests. See [implementation changes](opportunity_miner.md#implementation-history)
for the module-by-module account of what changed and why.


<a id="review-fixes-history"></a>

## Opportunity review and paper-role fixes

Implemented after [broader validation](opportunity_miner.md#broader-validation-history) exposed two
failures: reviewers accepting candidates that needed factual corrections, and
context-only papers inflating multiple-paper support.

### Behavior

Review now explicitly returns factual status, required corrections, and one
candidate-relative assessment per selected evidence paper. A correction,
`needs_correction` status, or `revise` decision creates a `requires_correction`
diagnostic. This veto overrides a simultaneous `accept` and is applied before
schema repair when recognizable in the response. The original candidate and
review payload remain in diagnostics. Uncertain factual accuracy also blocks
acceptance. There is no silent rewrite or automatic promotion: a corrected
candidate must receive a fresh original-page review.

The reviewer assigns each selected paper `supporting` or `context_only` relative
to the new opportunity, with rationale, selected evidence IDs, and original
page IDs. Code checks exact paper coverage and citation ownership. At least one
paper must support an accepted candidate. Upstream `supports`/`contradicts`
stances do not determine the new role; contradictory papers can both support a
new question about their discrepancy.

The output is now **`opportunities_v2`**, prompt
`opportunity_v2_review_roles`. `paper_ids` still retains every cited evidence
paper; `supporting_paper_ids`, `context_paper_ids`, and `paper_assessments` are
required new fields. `single_paper`/`multiple_papers` counts only supporting
papers. All selected evidence and previously reviewed contrary pages remain.
Legacy v1 outputs are not silently promoted to v2: re-run mining or explicitly
re-review their candidates. The validation tools read historical candidates
without treating historical acceptance as a new review.

Landscape construction, upstream reasoning, proposal logic, and concurrent
scheduling are unchanged. This adds structure to the existing review call,
not a sequential extra model pass per candidate.

### Offline validation

**149 tests passed, 13 subtests passed**; one existing FAISS/NumPy warning.
Fourteen new regressions verify correction signals independently, an accept
with corrections, a malformed review carrying a veto, uncertain acceptance,
context-only support counts with retained citations, role independence from
upstream stance, missing/duplicate/unknown roles, evidence/page ownership,
legacy review responses, and zero-support acceptance. Existing concurrency,
atomic budget, original-page, and stale-evidence tests continue to pass.

### Focused live validation

32 DeepSeek calls, no truncation, using the same saved real inputs and unchanged
source artifacts. Independent reviews were concurrent. This rechecks the changed
review behavior; it does not regenerate the already-tested landscapes or rerun
unchanged proposal generation.

| Check | Result |
|---|---|
| Eight positive category review controls | 8/8 accepted |
| Six synthetic negative controls | 6/6 withheld with correction diagnostics |
| Real author-attribution and already-answered probes | 2/2 withheld |
| Original inaccurate TokenWeave hardware candidate | 3/3 withheld; previously 3/3 accepted |
| AttentionPack-only question citing STAR-KV for exclusion | 3/3 accepted as single-paper support; STAR-KV retained as context |
| Shared AttentionPack/STAR-KV model-scale question | 3/3 retained as two supporting papers |
| Hardware-only wording correction | 2/3 accepted; 1/3 requested further scope corrections |
| Hardware plus reviewed-page scope correction | 3/3 accepted |

The hardware-only edit preserved an overbroad rationale saying the paper did
not test multi-node scales. One reviewer requested limiting that claim to the
reviewed page and accounting for the mentioned B200 appendix results. We kept
that failed expectation in the first regression artifacts, then created a
separate corrected candidate and fresh run. The production reviewer was not
weakened to pass the test. The two acceptances of the partially corrected version
also show that error detection remains variable: deterministic veto enforcement
cannot guarantee that an LLM notices every factual problem.

One fully corrected review initially assessed extra papers from the review-page
context. Code rejected that role coverage and allowed one schema/reference
repair; the resulting role list contained only the selected TokenWeave paper.
No substantive correction veto was repaired into acceptance.

An independent audit passed for 19 accepted output objects: 50 evidence references
and 77 page references, including support/context partitions, role citation
ownership, original source values/provenance, and artifact/page hashes.
Token totals: 242089 input, 177187 output, including 160550 reported reasoning
tokens. These are isolated process totals, not overlapping component snapshots.

### Artifacts and reproduction

- `/home/hema/research_runs/opportunity_controls_fixed/`: 19 review controls.
- `/home/hema/research_runs/opportunity_fix_regressions/`: nine real-candidate
  repetitions, preserving the partial-correction failure.
- `/home/hema/research_runs/opportunity_fully_corrected/`: three fresh reviews
  of the candidate with both hardware and scope corrected (four calls).
- `/home/hema/research_runs/opportunity_fix_audit.json`: independent audit.

Paid rechecks (use new output directories):

```bash
uv run python -m tools.validate_opportunity_controls --review-only --max-api-calls 28 --out /path/to/new/controls
uv run python -m tools.validate_opportunity_fixes --out /path/to/new/regressions
uv run python -m tools.validate_opportunity_fixes --fully-corrected-only --out /path/to/new/corrected
```

Scientific judgment remains model-dependent. Automatic narrowing/redrafting,
following unreviewed appendix references, semantic deduplication, and the later
novelty-search stage remain separate work. None of the earlier saved outputs
was rewritten or relabelled to hide baseline failures.


<a id="boundary-validation"></a>

## Opportunity Miner boundary and full-context validation

Run on 2026-10-03 using the configured DeepSeek `deepseek-flash` endpoint.
The focused checks passed: 12 synthetic reviews and three real-corpus miner
runs, across 34 API attempts. No provider errors or truncated responses occurred.
Peak concurrent API requests: four. The full offline suite passed 159 tests and
13 subtests (one existing FAISS/NumPy deprecation warning).

### Scope and setup

This validates architecture section 6: whether motivating evidence supports an
unresolved question, with accurate conditions, attribution and scope. It does
not judge scientific value, predict future discoveries, or certify novelty.
Direction generation, novelty retrieval and the research critic remain separate
stages with their own responsibilities.

Synthetic cases use clearly marked fictional pages and hand-authored upstream
reviewed observations. Each case received two independent live reviews. The real
runs reuse saved landscapes and reviewed reasoning from the earlier MoE,
KV-cache and RAG validations; retrieval, extraction, landscape construction and
cross-paper reasoning were not rerun here.

The miner used current defaults: one proposal context containing all reviewed
sources, 500,000-token input/output allowances, and the configured provider output
cap of 393,216. This was a functional run, not a 500k-token load test. The existing
five-candidate proposal cap remains independent of the token allowances.

### Evidence-boundary results

| Control | Expected behavior | Observed |
| --- | --- | --- |
| Compression changes entropy; separate caching work relates entropy to latency | Accept the inferred joint-effect question without claiming a demonstrated joint benefit | Accepted 2/2 |
| Same evidence, but candidate claims a proven combined benefit | Withhold unsupported benefit | Requires correction 2/2 |
| Opposite latency effects under nominally matched settings, unknown seed/warmup | Accept a scoped discrepancy question | Accepted 2/2 |
| Different GPUs, workloads and batch sizes; ask which differences explain the effects | Accept a question that preserves those differences | Accepted 2/2 |
| Same differing settings falsely described as identical experiments | Withhold false contradiction | Requires correction 2/2 |
| Reviewed pages explicitly say tail measurements exist elsewhere; candidate says never measured | Withhold missing-evaluation claim | Requires correction 2/2 |

The last control does not require appendix ingestion. Missing extracted material
is not proof of missing experiments. Reviewer suggestions for narrower wording
were retained as diagnostics; the rejected candidate was not silently rewritten
or promoted. A page-availability gap alone does not establish a scientific gap.

### Real-paper runs

| Corpus | Reviewed source objects in the single proposal context | Proposed | Accepted | Withheld | API attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| MoE inference, 3 papers | 1 finding containing multiple evidence records | 5 | 4 | 1 | 8 |
| KV-cache compression, 3 papers | 12 | 5 | 4 | 1 | 6 |
| RAG grounding, 3 papers | 8 | 5 | 4 | 1 | 8 |

All three saved results use `opportunities_v2`. Each has one accepted opportunity
with reviewed support from multiple papers; the other three are single-paper
opportunities. Paper roles describe support for the specific candidate, not a
count of all cited or reviewed papers.

Concrete withheld candidates:

- **MoE:** the candidate described a separate four-H100 machine; the page says
  TP-4 uses four GPUs of the eight-H100 machine. Review also required tighter
  wording about untested regimes and the additional hardware results mentioned
  on the page.
- **KV-cache:** an apparent disagreement about which paper reported a fused
  kernel was already resolved by the original AttentionPack pages. A landscape
  attribution error did not become a research contradiction.
- **RAG:** a candidate attributed a “weakest instrument” statement to the paper,
  but the supplied pages did not support that attribution. Even though its core
  question was plausible, the candidate was withheld as written.

Four bounded format/reference repairs succeeded: two proposals included an extra
`title` field, and two acceptance reviews initially omitted a required paper-role
assessment. These were not retries that erased substantive correction vetoes.
Actual simultaneous reviews peaked at three for MoE and four each for KV and RAG.

### Saved-output audit

All 18 accepted objects (six synthetic positive reviews plus 12 real opportunities)
passed the audit: 63 evidence references and 70 reviewed-page references, counted
per output rather than as unique documents. Checks covered:

- JSON round-trip through the v2 schema and exact landscape/reasoning hashes.
- Every upstream reviewed source reaching each real proposal call.
- Candidate/source/evidence ownership and landscape refinement IDs.
- Original artifact hashes, value/provenance pointers, source values and locations.
- Original page hashes and paper ownership of assessment citations.
- Supporting/context-only role partitions and derived support labels.
- Preservation of `literature_novelty: not_assessed`.

Offline audit tests also demonstrated rejection of changed original pages and
incorrect paper-role partitions. Hash/reference correctness does not establish
that every model judgment is scientifically correct. The sample is small and
model review remains variable. Related accepted MoE questions still overlap in
scope; full-context input does not guarantee semantic deduplication or exhaustive
opportunity coverage. The five-candidate cap also means these counts are not a
measure of all possible unresolved questions.

### Reproduction and artifacts

```bash
uv run python -m tools.validate_opportunity_boundaries --out /path/to/new/run
uv run pytest -q
```

The harness limits concurrent requests to four and total attempts to 48 by
default; it exits nonzero on failed expectations or audit errors. Its default
input paths refer to the private saved runs used here. Shared paper data remain
read-only.

Artifacts: `/home/hema/research_runs/opportunity_boundaries_live/` contains
`summary.json`, call timing and raw request/response files, synthetic inputs and
verdicts, and each real corpus's input and `opportunities.json` files. Per-miner
usage deltas overlap during concurrent execution; the process aggregate in
`summary.json` is authoritative: 331,897 prompt tokens, 81,408 cached prompt
tokens, 167,812 completion tokens, including 145,498 reasoning tokens.

No production miner logic was changed in this validation pass. Added files are
the reusable boundary harness, audit tests and this report; the synthetic fixture
helper now accepts a custom case. The associated token-budget and
full-context implementation changes are documented module by module in
[Implementation updates](opportunity_miner.md#implementation-history).
