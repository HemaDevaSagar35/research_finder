# Direction Generator (`directions/`)

> **Current contract: `directions_v3`.** The 2026-10-04 independent correctness
> judge entry below supersedes the earlier unreviewed output contract. The v2 scope
> correction still supersedes the v1 protocol fields and dependency graph below.
> The earlier implementation descriptions and validation reports are retained as
> history. Current output contains research directions, hypotheses and concise
> suggested tests, not detailed execution plans.

New module implementing architecture sections 7–8 and component contracts 10–12:
Future Direction Generator, Hypothesis Generator and Experiment Generator.
It asks what research could attack an accepted opportunity. The existing miner
supplies the gap; this stage does not rediscover it.

## Approach and boundaries

Inputs are the exact `Landscape`, `CrossPaperReasoning`, and `opportunities_v2`
`MiningResult`, plus original paper artifacts accessed through `PaperStore`.
Each accepted opportunity gets one model call containing landscape context,
its reviewed cross-paper sources, paper projections and original reviewed pages.
Direction, hypotheses and experiments are generated together so that mechanisms,
conditions and tests are consistent. Independent opportunities run concurrently.

One direction per accepted opportunity is the initial implementation choice,
not a scientific restriction of the architecture. Each direction contains one
to four hypotheses and one to four experiments by default. One experiment may
test several hypotheses. The first experiment is the proposed cheapest
informative test; optional followups refer to earlier experiments. No output
portfolio ranking or forced selection of three to five directions happens here.

The model must distinguish factual motivating rationale from proposed mechanisms
and effects. Every hypothesis specifies condition, intervention, expected effect,
mechanism, assumptions, motivating evidence IDs and a falsification criterion.
Experiments specify setup, intervention, baselines, controls, metrics, resource
requirements, cost rationale and what positive/negative results would teach.
The first experiment is the code-assigned recommended next step. Proposals also
include risks, uncertainties, scope and direction-level falsification criteria.

Reference checks are deterministic, not a second scientific-review call.
`evidence_validation=references_and_artifacts_checked` does not mean the new
rationale is semantically verified. `hypothesis_status=proposed_untested`,
`scientific_review=not_assessed` and `literature_novelty=not_assessed` preserve
this distinction. Actual causal plausibility, experimental usefulness/cost,
scientific value, and prose consistency still require later review. Novelty
signature/search, refinement, the research critic and ranking are downstream.

## Execution and integrity checks

1. Normalize inputs and verify exact landscape/reasoning hashes and topics.
   Reject duplicate opportunity IDs, duplicate upstream source IDs and unsafe
   paper directory IDs before model calls. Only accepted opportunities are used;
   miner diagnostics do not become direction inputs.
2. Match each opportunity's source/refinement IDs and selected evidence to the
   actual reviewed reasoning items. Validate supporting/context-only roles and
   their evidence/page ownership. Preserve the exact opportunity snapshot;
   a context-only citation never becomes an additional supporting paper here.
3. Require all previous source-review pages and cited evidence pages to remain
   in the opportunity's context. Reload original artifacts; validate hashes,
   values, provenance pointers and page locations. Reload and hash-check original
   pages. Include paper projections as context, not as independently verified
   claims. Changed/missing artifacts produce diagnostics before generation.
4. Enforce the page budget on the complete reviewed context. Enforce the token
   allowance on the complete prompt, schema included. Exceeding a budget does
   not silently drop evidence, shrink the opportunity or split its context.
5. Make the asynchronous generation call. Validate rationale evidence/page IDs
   and cited-paper coverage, hypothesis evidence IDs, unique hypothesis and
   experiment IDs, experiment dependencies and hypothesis test coverage. At
   least one rationale citation must come from a supporting paper. All
   hypotheses must have tests; every followup depends on an earlier experiment.
6. Assign run-local `dir-NNN`, hypothesis and experiment IDs in code, remapping
   links consistently. Return `directions_v1` with exact input hashes, opportunity
   snapshots, original paper hashes and reviewed-page references, proposals,
   diagnostics, coverage, settings, prompt version, calls and usage.

IDs are scoped to this generation result and input order, not global identities
across changed input lists. Model reference/schema errors and truncation get a
bounded repair. A valid abstention is retained without a repair or fabricated
direction. No automatic scientific critique, novelty verdict or substantive
refinement loop is introduced.

## Module files and implementation history

### 2026-10-03 — initial implementation after `8408d4f`

This is a new module, not an edit to the contributor's Landscape Builder or a
rewrite of the Cross-Paper Reasoner/Opportunity Miner.

| File / component | Implementation |
| --- | --- |
| `directions/__init__.py` | New package. |
| `directions/schemas.py` | `GroundedRationale`, `Hypothesis`, `Experiment`, `DirectionDraft`, `GenerationResponse`, code-owned `Direction`, diagnostics and `GenerationResult`. Strict schemas forbid extra fields; draft validation checks the experiment graph and hypothesis coverage. |
| `directions/generator.py` — `Settings`, `DirectionGenerator.run()` | Exact three-stage lineage, accepted-opportunity scheduling, independent concurrent processing, code-assigned IDs and deterministic output order. |
| `_source_context()` | Cross-check upstream references, evidence identity and origin, paper roles and retained review context. |
| `_payload()`, `_resource()` | Threaded local/S3 artifact access with per-resource locks; source/hash/page checks; full context assembly. |
| `_generate()`, `_validate_draft()`, `_call()` | Coupled direction/hypothesis/experiment prompt, structural/reference validation, complete-prompt budget, bounded repairs, semaphore and atomic attempt budget. |
| `main()` | `uv run` CLI, JSON output and diagnostic-aware exit status. |
| `tests/test_direction_generator.py` | Offline contract, provenance, experiment-graph, budget, repair, lifecycle and concurrency checks using clearly synthetic data/stub responses. |
| `tools/validate_direction_generator.py` | Reproducible paid smoke harness over the three saved real opportunity sets, four concurrent requests, default 24-call total cap, saved inputs/raw calls/output and consumer handoff checks. |

Reuses `PaperStore`, `paper_card()` and evidence candidates from `reasoning.evidence`,
`AttemptBudget` and token estimates from `reasoning.budget`, `digest()` from the
miner, and the shared async LLM client. No production files inside `landscape/`,
`reasoning/`, `opportunities/` or `llm_client/` are changed by this implementation.
Existing project/module indices receive links; this file holds all generator
implementation history and validation reports.

## Configuration and usage

Provider: explicit argument, then `DIRECTION_PROVIDER`, then general `PROVIDER`.
Model: explicit argument, then `DIRECTION_MODEL`, then the selected provider's
default. The shared client applies a configured provider output cap; an
application allowance does not enlarge provider capacity.

| Setting / CLI | Default | Meaning |
| --- | ---: | --- |
| `--concurrency` | 4 | Simultaneous model calls per generator |
| `--max-calls` | 40 | All attempted generation and repair requests |
| `--max-hypotheses` | 4 | Hypotheses per direction |
| `--max-experiments` | 4 | Initial plus followup experiments per direction |
| `--max-pages` | 20 | Complete original-page context per opportunity |
| `--max-input-tokens` | 500000 | `DIRECTION_MAX_INPUT_TOKENS`; whole-prompt estimated limit |
| `--max-output-tokens` | 500000 | `DIRECTION_MAX_OUTPUT_TOKENS`; bounded by provider cap |
| `--repair-rounds` | 1 | Additional schema/reference/truncation repair attempts |

Explicit settings override environment defaults. Page, hypothesis and experiment
limits remain separate from token allowances.

```bash
uv sync
uv run python -m directions.generator \
  --landscape landscape.json \
  --reasoning reasoning.json \
  --opportunities opportunities.json \
  --root /srv/research_finder/markdown \
  --out directions.json
```

Library:

```python
from pathlib import Path
from directions.generator import DirectionGenerator
from reasoning.evidence import PaperStore

generator = DirectionGenerator(PaperStore(Path('/srv/research_finder/markdown')))
result = await generator.run(landscape, reasoning, opportunities)
```

Create a new generator and store per run/request. An injected async chat function
is caller-owned; an internally created client is closed after all tasks finish.
The attempt counter includes failed calls. Account/provider rejections stop
queued API calls; already-running calls can finish. One opportunity's failure
does not discard successful directions from others. The CLI saves output then
returns 0 for complete success, 1 for diagnostics/partial output (including
abstention or no accepted inputs), and 2 for a fatal provider rejection. Invalid
input lineage raises before generation/output assembly.

Store instances cache files, so reusing an externally preloaded store could
hide on-disk changes since it loaded them. The documented request-local store
lifecycle avoids that ambiguity. Shared global usage snapshots can overlap
across concurrent pipeline requests; per-request accounting remains a service
integration concern. Live harness process totals are authoritative for that run.

## Validation

```bash
uv run pytest -q tests/test_direction_generator.py
uv run python -m tools.validate_direction_generator --out /path/to/new/run
```

Offline tests check actual control flow and data integrity; fake model responses
do not establish scientific correctness. Completed live outcomes and the citation-scope correction are recorded below. A near-limit input load and deployed service behavior
are outside this initial module validation.


### Initial live smoke run and citation-scope correction

The first live run reused all 12 accepted opportunities from the MoE, KV-cache
and RAG sets. It completed 15 calls (12 generation, three repair), with peak
concurrency four and no truncations or provider errors. Eleven opportunities
produced directions: four MoE, three KV-cache and four RAG, totaling 43
hypotheses and 43 experiments. One KV-cache proposal remained invalid after its
repair because it cited an unselected upstream evidence record. The harness
correctly exited nonzero; these results remain preserved in
`/home/hema/research_runs/direction_generator_live/`.

Inspection found a prompt ambiguity in the initial implementation: complete
upstream reasoning objects exposed original unnamespaced evidence IDs, including
records that the accepted opportunity had not selected. The generator's stricter
reference boundary correctly rejected them, but the prompt did not make that
boundary sufficiently clear. Two repairs succeeded; the third merely prefixed
the unselected ID and remained invalid.

The correction in `directions/generator.py`:

- `_source_context()` now projects cross-paper statements, conditions, tension
  sides/explanations and review notes as context, with only the opportunity's
  selected evidence IDs attached. It no longer exposes a second structured
  citation namespace through upstream evidence objects.
- `_payload()` explicitly supplies `citable_evidence_ids` and `citable_page_ids`.
- The prompt permits only those IDs, instructs omission/abstention for unsupported
  motivating claims, and forbids substituting a different record merely to pass.
- Reference-repair feedback includes the exact allowed IDs. The acceptance checks
  remain strict; no invalid citation is silently remapped or dropped.
- Prompt version is `direction_v1_citation_scope`.

`test_cross_paper_context_does_not_offer_unselected_citation_ids` reproduces the
ambiguity with an extra upstream record and verifies that generation context
exposes only selected IDs. The original failed live run is retained separately
from the corrected run; a later successful run is not evidence of infallible
citation behavior. Prose support and scientific quality still require later
review, even when citation IDs resolve.


### Corrected live rerun — completed 2026-10-04

The corrected run reused the same 12 accepted opportunities with fresh model
calls. All three corpora completed without remaining diagnostics:

| Saved opportunity set | Accepted inputs | Directions | Hypotheses | Experiments | API attempts |
| --- | ---: | ---: | ---: | ---: | ---: |
| MoE inference | 4 | 4 | 16 | 15 | 4 |
| KV-cache compression | 4 | 4 | 16 | 16 | 5 |
| RAG grounding | 4 | 4 | 16 | 16 | 4 |
| Total | 12 | 12 | 48 | 47 | 13 |

Peak concurrency was four. There were no citation repairs, provider errors or
truncated responses. One KV-cache response omitted `informative_outcomes` from
three followup experiments; the existing bounded schema repair supplied them.
No validation rule was relaxed. The successful rerun does not prove that model
outputs will always conform without repairs or abstention.

Examples of generated directions include top-k-conditioned expert co-clustering,
capacity-granularity boundary mapping, controlled high-sigma stress testing for
MOMENTKV, and experiments separating prompt output contracts from conflict-aware
answer/refuse behavior. These are model-generated research proposals, not
endorsed findings or assessed-novel contributions. Their first tests use routing
trace analysis, synthetic tensors, or bounded inference comparisons where
appropriate; the cheapest/useful ordering remains a model judgment to assess
later, not a measured cost guarantee.

A fresh-store audit of the persisted outputs passed for all 12 directions:
51 upstream evidence references and 58 original-page references, counted per
output. It checked opportunity snapshots and hashes, actual paper hashes,
source/provenance pointers and values, page hashes, rationale citation ownership,
code-assigned IDs, and experiment/hypothesis links. Schema round-trip checks
preserve explicit untested/unassessed statuses.

The final offline suite passes **199 tests plus 13 subtests**, including **40 new
generator tests**. One existing FAISS/NumPy deprecation warning remains. Tests
include context-only citations, missing/stale artifacts, incorrect lineage,
experiment cycles and coverage, budgets, bounded repair, abstention, transient
partial failures, fatal-provider queue stopping, owned-client cleanup,
concurrency, and the live-discovered citation-scope regression.

Artifacts: `/home/hema/research_runs/direction_generator_citation_scope_live/`
contains saved inputs, `calls.json`, raw calls, per-corpus `directions.json`,
`summary.json`, and `artifact_audit.json`. Corrected-run process totals: 332798
prompt tokens (35840 cached), 183984 completion tokens including 111957 reasoning
tokens. The baseline and corrected runs together used 28 calls; the baseline
failure is preserved rather than counted as a clean success.

No novelty retrieval, candidate comparison/refinement, research critic, ranker
or service endpoint was implemented in this change. The stage is ready to feed
those next components through `directions_v1`; it does not declare the generated
research scientifically correct or novel.


### 2026-10-04 — tension preservation and generation-fidelity checks

These follow-up checks target two remaining generator behaviors: carrying both
sides of unresolved reasoning tensions, and keeping the selected opportunity
and untested status intact in generated prose. They do not introduce a runtime
critic or a scientific-value/novelty benchmark.

Added validation files:

- `tools/direction_validation_cases.py`: four fictional two-paper controls with
  explicit facts and predeclared inspection rubrics. Two use actual `Tension`
  objects with separately attributed sides and an untested explanation; one
  concerns compression and caching tested separately; one adds an unrelated
  image-quantization landscape item beside the selected tail-latency gap.
- `tests/test_direction_fidelity.py`: eight offline checks cover original-page
  delivery, both tension assertions and conditions, explanation hypothesis flags,
  selected citation IDs, preserved opportunity snapshots, and rejection of
  dropped/changed pages or side evidence before generation.
- `tools/validate_direction_fidelity.py`: runs each case twice using the real
  generator and configured DeepSeek endpoint, with four concurrent calls,
  default 16-attempt total cap, and two hypotheses/experiments per direction.
  Token allowances retain their 500000 defaults. Inputs and upstream acceptance
  are hand-authored fixtures, not live reasoner/miner decisions.

```bash
uv run pytest -q tests/test_direction_fidelity.py
uv run python -m tools.validate_direction_fidelity --out /path/to/new/run
```

The harness reports structural checks separately from semantic fidelity. Passing
schema/reference checks does not automatically pass the wording checks. Each
saved proposal is inspected against the fixed fixture facts and rubric; this is
an assistant inspection, not an independent scientific expert or another LLM
judge call. The required distinctions are original results versus proposed
effects, unexplained discrepancies versus established causes, and the chosen
gap versus unrelated landscape context. Outcomes are recorded below.

#### Baseline findings and focused instruction changes

The first run, `/home/hema/research_runs/direction_fidelity_live/`, produced
8 directions from 8 calls, with no repairs, truncations or API errors and peak
concurrency 4. All structural checks passed. Inspection found both tension sides,
condition differences, untested joint effects and the selected gap preserved in
all eight outputs, but **this was not a clean overall fidelity pass**:

- `untested_interaction_0` called three factorial control arms “already-characterized”
  and misstated the cache comparison. The supplied sources establish compression's
  entropy change and cache latency at two entropy settings, not all latency/locality
  measurements across those arms.
- `tension_different_conditions_1` linked its initial, G1-only experiment to a
  cross-GPU hypothesis. Its followup could test that hypothesis; the initial
  experiment could not directly do so.

Edited existing `directions/generator.py`: prompt version
`direction_v1_measurement_scope` now explicitly preserves the reported metric,
experimental arm and comparator; new control measurements must be presented as
proposals. It also requires that an experiment's actual setup can test each
linked hypothesis, distinguishing preliminary information from a direct test.
This changes generation instructions only; contracts, orchestration, reference
validation and stage boundaries remain as previously documented. These semantic
requirements are not guaranteed by schema validation.

The baseline `semantic_inspection.json` records the assistant's findings, including
both failures. The original four checks per case remain recorded separately from
these additional findings; the baseline is not retrospectively relabeled a success.
The complete offline regression suite after the instruction change passes
**207 tests plus 13 subtests** (48 generator tests including the eight new checks).
The existing FAISS/NumPy deprecation warning remains.

#### Fresh-run results and remaining limitations

The fresh run in
`/home/hema/research_runs/direction_fidelity_measurement_scope_live/` produced
**8 directions, 16 hypotheses and 16 experiments in 8 calls**, with peak
concurrency 4, no repairs, no truncations and no API errors. Process usage:
38824 prompt tokens (13952 cached), 81853 completion tokens including 56470
reasoning tokens. Baseline plus fresh run used 16 calls in total.

All eight outputs passed structural validation and the four original inspection
checks for their case. Both reports and their conditions survived the tension
cases, proposed causal explanations stayed untested, separate compression/cache
results were preserved, and the tail-latency question was not replaced by the
unrelated landscape item. Neither of the two original mistakes recurred in this
fresh sample. This is evidence from these samples, not a semantic guarantee.

**The broader experiment-draft inspection still has two failures, so this is not
an overall semantic-quality pass (6/8 outputs without additional findings):**

- `untested_interaction_0`: E1's controls assign C10 both approximately 4-bit and
  2-bit entropy. C10 is the compression-on/cache-off arm; the control specification
  contradicts itself.
- `untested_interaction_1`: H2 assumes cache B is not at a hit-rate ceiling in the
  A-only arm, although that arm disables B. The proposed comparison must define
  applicable metrics for each arm; trace reuse could be measured without a cache,
  but B's live hit rate cannot simply be assumed in that arm.

These are inconsistencies in generated experiment proposals, not invented joint
results attributed to the papers. They remain recorded in `semantic_inspection.json`
with exact fields and excerpts. `summary.json` links that inspection and retains
separate structural and manual verdicts. No further samples were substituted to
hide the failures, and no automatic runtime critic was added.

The two requested preservation/fidelity checks are complete, with the broader
experiment-quality limitations explicitly open. The architectural novelty stage
is still the next unimplemented component; generated experiment drafts must not
be treated as execution-ready solely because their schemas and references pass.
The later research critic must assess experimental coherence as well as scientific
merit. Improving or validating that behavior is separate from these source-fidelity
checks. The synthetic sample is deliberately small and is not a general quality
estimate or an expert evaluation of scientific usefulness.


### 2026-10-04 — experimental arm and metric consistency follow-up

The preceding live run exposed two concrete drafting errors: conflicting entropy
values for one arm and a cache-hit-rate assumption in a cache-disabled arm. At the
user's request, the existing `directions/generator.py` generation instructions now
use prompt version `direction_v1_arm_consistency` and require:

- One definition per experimental arm in `setup`, carried consistently through
  hypotheses, controls, metrics, baselines and outcome comparisons.
- Reported values, proposed targets and unknown joint-condition values to remain
  distinct; different target values must identify their different subconditions.
- Metrics to name the arms where they apply. An inactive component's live metric
  is unavailable, not zero or saturated. Trace reuse and hypothetical simulation
  can be proposed separately with explicit definitions and assumptions.
- A final consistency check within the existing generation call, including
  assumptions and falsification criteria, before returning the proposal.

This is a prompt change within the existing module, with no schema change, extra
API stage, or automatic scientific critic. `tools/direction_validation_cases.py`
now adds predeclared arm-consistency and experiment-applicability checks to every
case, plus explicit entropy and disabled-cache checks for the interaction case.
The saved rubric remains a manual inspection rubric, not a word-matching test.

Validation reruns all four cases twice, rather than only the two failed outputs,
using the existing concurrent harness. Original artifacts and failure reports
remain preserved. Fresh artifacts are in
`/home/hema/research_runs/direction_fidelity_arm_consistency_live/`.


#### First consistency rerun and related findings

`direction_fidelity_arm_consistency_live/` produced eight structurally valid
outputs in eight calls (peak concurrency 4; no repairs, truncations or API
errors). Neither the contradictory entropy assignment nor the live-cache-metric
in a disabled-cache arm recurred. The broader manual inspection nevertheless
found four other inconsistencies, preserved in `semantic_inspection.json`:

- `tension_different_conditions_0` fixes the seed but requests variance across
  repeated seeds, without defining a sweep or blocking arrangement.
- `untested_interaction_1` links a B-on/B-off hypothesis to a followup with B on
  in every arm; it investigates a related mechanism without the required contrast.
- `untested_interaction_0` predicts non-inferiority but uses a two-sided equivalence
  failure criterion that could reject an improvement.
- `gap_with_distraction_1` describes a reversed ranking as stable across loads.

The original two targeted mistakes are therefore absent in this sample, but the
broader result is not a clean pass. Process usage was 40824 prompt tokens (18560
cached), 108157 completion tokens including 73490 reasoning tokens.

The same existing generator instructions were further refined to
`direction_v1_experiment_consistency`: distinguish fixed-seed repetition from
seed sweeps/blocks; require every experiment link, including followups, to contain
the hypothesis's treatment/control contrast; reconcile predictions, falsification
and outcome interpretations; keep infeasible or missing measurements untested.
Descriptions should reuse arm definitions concisely. The validation rubric now
explicitly checks prediction/criterion/outcome agreement too. There is still one
coupled generation call per opportunity, with the existing bounded schema repair;
no semantic judge call or runtime critic has been introduced.

A separate fresh run tests the final instructions on all four controls twice:
`/home/hema/research_runs/direction_fidelity_experiment_consistency_live/`.


#### Final rerun result — targeted mitigation, broader consistency still open

The final `direction_v1_experiment_consistency` run produced **8 directions,
16 hypotheses and 16 experiments in 9 calls**, with peak concurrency 4. The ninth
call was the existing bounded schema repair: `tension_matched_0` initially returned
strings where `baselines`, `metrics` and `controls` require lists in both
experiments. Repair succeeded. There were no API errors or truncations.
Process usage: 51812 prompt tokens (21888 cached), 124903 completion tokens
including 90876 reasoning tokens. This follow-up used 17 calls across its two
runs; earlier fidelity runs remain separately documented above.

All eight final outputs pass structural validation and the original four
source-preservation checks for their cases. Neither original targeted failure
recurred: no incompatible entropy assignment to a single arm, and no live cache
hit rate in a cache-disabled arm. One interaction output explicitly uses a
simulated-cache screen on traces with live caching disabled, followed by a live
experiment; it correctly labels the simulation and gives it its own hypothesis.
The earlier followup/hypothesis-link failure also did not recur in this sample.

**The expanded manual rubric still fails in four outputs:**

| Output | Remaining inconsistency |
| --- | --- |
| `tension_matched_1` | E2 defines matched seeds, then one control says seed differs between the two arms of the matched pair. |
| `untested_interaction_0` | E1 defines latency as either time per token or tokens per second, although those quantities have opposite improvement directions; the subsequent negative latency contrast requires a fixed time-based metric. |
| `gap_with_distraction_0` | H1 predicts both p95 and p99 improvement but uses only p99 to declare support/rejection. H2 permits stability in most windows but rejects any ranking change. |
| `untested_interaction_1` | H2 predicts being closer to an entropy-matched control but treats any systematic difference from that control as a mediation failure; closeness and equality are different criteria. |

`tension_different_conditions_1` also calls a one-anchor reversal both support and
partial/mixed support; this is recorded as wording needing clarification rather
than counted as an additional failure. Every verdict and the exact fields are
preserved in `semantic_inspection.json`; `summary.json` links it and does not
present structural success as semantic success. No generated output was manually
corrected, discarded or replaced to obtain a clean result.

The regression suite with the final instructions remains **207 tests plus 13
subtests passed**, with the same existing FAISS/NumPy warning. No offline test
claims to prove natural-language consistency by checking for prompt keywords.

This entry supersedes the preceding open status of the two specific entropy/cache
mistakes only for the tested samples. The prompt-level mitigation is implemented
and retested; general experimental coherence remains open. These findings support
keeping proposals explicitly untested and retaining the planned later critic;
they do not establish that that future critic would catch these failures, nor
that any generated experiment is ready to execute. Broader enforcement would
need a separately designed validation contract or review mechanism, rather than
counting schema success or further prompt wording as a guarantee.


### 2026-10-04 — scope correction: tangible directions and suggested tests (`directions_v2`)

After checking architecture sections 7, 8 and 16 and component definitions 10–12,
the user clarified the intended scope: generate a tangible direction with
hypotheses that can develop through novelty comparison, refinement and critique.
The architecture includes suggested initial experiments, but does not require a
detailed execution protocol. The v1 implementation over-specified those suggestions;
the attempted experimental-setup validation expansion is not being implemented.

#### Current behavior and exact module changes

- **Existing `directions/schemas.py`:** `Experiment` now means a suggested test.
  Its content is `objective`, `comparison`, `observations`, `why_this_test`, and
  `informative_outcomes`, alongside its ID, initial/followup stage and hypothesis
  links. Removed mandatory `setup`, `intervention`, `baselines`, `metrics`,
  `controls`, `resource_requirements`, `cost_rationale`, and `depends_on`.
  Observations describe what to examine; they do not define a measurement protocol.
  The first suggestion remains initial, later suggestions are followups, IDs must
  be unique and references valid, and every hypothesis has a suggested test.
  The experiment dependency graph is removed. The persisted contract is now
  **`directions_v2`**.
- **Existing `directions/generator.py`:** prompt version
  `direction_v2_suggested_tests` asks for a concrete research question and mechanism,
  condition/intervention/effect hypotheses, and concise proposed comparisons with
  meaningful observations and supporting/contrary outcomes. It removes the v1
  requirements to prescribe arm definitions, seed policies, instrumentation,
  hardware allocation and resource estimates. Relevant conditions from the papers
  are still retained. The prompt prioritizes an informative economical first test
  and permits useful followups without forcing maximum counts. Generation no longer
  remaps experiment dependencies. It still assigns IDs and the recommended first
  test, and preserves source validation, evidence/page references and lineage.
- **Existing `tests/test_direction_generator.py`:** fixtures now use research-test
  suggestions. The handoff checks v2 and the new fields; the obsolete cycle test
  becomes a legacy-protocol rejection test. New cases reject an explicitly labeled
  v1 result and missing comparison, observations or informative outcomes.
- **`tools/direction_validation_cases.py`:** retains the four original source-fidelity
  controls and replaces protocol-level inspection criteria with direction specificity,
  relevant suggested comparisons/observations, and research-test scope. This changes
  the evaluation target in accordance with the user's scope correction; historical
  failures are not relabeled as passes.
- **`tools/validate_direction_fidelity.py` and
  `tools/validate_direction_generator.py`:** persisted-output checks now require v2.
  The synthetic harness retains manual semantic inspection separately from schema
  success. `tests/test_direction_fidelity.py` continues testing source delivery and
  rejection of damaged upstream evidence.

Directions retain evidence-backed rationale, a proposed mechanism, scope,
assumptions, testable hypotheses and their falsification criteria, risks and
uncertainties. Hypotheses keep their existing contract. Suggested tests may
investigate multiple hypotheses, but precise semantic relevance of those links
is not established merely by valid IDs. The accepted opportunity is preserved
as the input snapshot. Independent opportunities still run concurrently; the
500000 token allowances and existing operational budgets are unchanged.

#### Contract and compatibility

The `experiments` list and `recommended_next_experiment_id` names are retained,
but their content now represents suggestions. A test can look like:

```json
{
  "experiment_id": "dir-000-e01",
  "stage": "initial",
  "hypothesis_ids": ["dir-000-h01"],
  "objective": "Test whether compression helps expert caching through routing changes.",
  "comparison": "Compare caching with and without compression on the relevant workload.",
  "observations": ["Routing entropy", "Cache locality", "Latency"],
  "why_this_test": "A direct comparison tests the proposed interaction before exploring a broader method.",
  "informative_outcomes": "Joint locality and latency gains support the idea; better locality without lower latency suggests that overhead offsets the benefit."
}
```

The v2 reader rejects explicitly labeled v1 results and legacy protocol fields.
There is no silent conversion of old proposals. Existing saved v1 artifacts remain
unchanged; regenerate from their unchanged landscape/reasoning/opportunity inputs
when v2 output is needed. No downstream production consumer of direction results
was found beyond the updated tests and validation harnesses. Future novelty and
refinement consumers should explicitly use v2.

#### Validation and remaining limits

- **52 focused generator tests passed; full suite: 211 tests plus 13 subtests.**
  The existing FAISS/NumPy deprecation warning remains. Checks include evidence
  integrity, lineage, citations, concurrency, bounded repair, budgets, new contract
  handoff, rejection of old protocol output and required suggestion substance.
- **Synthetic live run:** eight outputs from eight calls, no repairs, API errors
  or truncations, peak concurrency 4. Produced eight directions, 16 hypotheses
  and 16 suggested tests. Saved artifacts:
  `/home/hema/research_runs/direction_v2_synthetic_live/`.
- Manual inspection found all eight preserve the original fixture facts and stay
  at the suggested-test level. A comparison can name the combinations needed to
  study an interaction; it does not prescribe an execution matrix, new numeric
  protocol settings, hardware plan or cost estimate. Warmup/seed variation can
  remain the research question in a fixture where the source explicitly identifies
  those unknowns; the generator does not prescribe their values or run schedules.
- Inspection also records remaining refinement issues rather than declaring a
  blanket semantic pass. Three outputs over-associate a first/followup suggestion
  with a hypothesis whose full comparison is in another suggestion; one
  direction-level falsification statement could reject a useful attribution
  result; one hypothesis needs a sharper prediction. Exact notes are in
  `semantic_inspection.json`; `summary.json` distinguishes source/scope checks
  from scientific validity. These remain starting proposals for later refinement
  and critique, not verified discoveries or execution-ready experiments.
- Synthetic-run usage: 38544 prompt tokens (13312 cached), 66112 completion tokens
  including 47987 reasoning tokens. This is a small controlled sample, not a
  general scientific-quality evaluation.
- **Saved real-paper inputs:** an offline audit passed for all 12 accepted
  opportunities across MoE, KV-cache and RAG-grounding inputs, checking original
  evidence loading, page/artifact hashes, source lineage and payload delivery.
  It used an injected abstaining stub and made zero network calls; this is not
  real-paper model-output validation. Report:
  `/home/hema/research_runs/direction_v2_corpora_offline/summary.json`.
- A requested tool execution for supplementary real-paper live validation was
  rejected by automatic approval review because it could not confirm authorization
  to export the saved opportunities/source excerpts to DeepSeek. That API run did
  not execute and was not bypassed. It remains separate from the completed synthetic
  live run and offline real-input audit; explicit approval is needed to run it.

Novelty signatures/search/comparison remain the next unimplemented stage. Candidate
refinement can later sharpen hypotheses and update the associated suggested tests;
this scope correction neither implements that stage nor claims novelty or scientific
review for generated candidates. All earlier v1 findings remain in this document
and their original artifact directories.


### 2026-10-04 — Authorized real-paper live validation of `directions_v2`

This entry supersedes the pending-authorization status above. The user explicitly
confirmed the described export of the 12 saved opportunities and source excerpts
to DeepSeek. The authorized run completed against the configured DeepSeek endpoint;
no further approval was requested. Only Direction Generator ran, reusing the saved
landscape, cross-paper reasoning and accepted opportunity artifacts.

Reproduction:

```bash
uv run python -m tools.validate_direction_generator --out /home/hema/research_runs/direction_v2_corpora_live
```

The output directory must be new when rerunning. The harness uses a shared async
client and semaphore, with four concurrent calls across all three corpora.

| Saved corpus | Accepted opportunities | Directions | Hypotheses | Suggested tests | API calls |
|---|---:|---:|---:|---:|---:|
| MoE | 4 | 4 | 14 | 13 | 4 |
| KV cache | 4 | 4 | 15 | 15 | 4 |
| RAG grounding | 4 | 4 | 14 | 13 | 4 |
| Total | 12 | 12 | 43 | 41 | 12 |

All 12 outputs passed the persisted v2 contract and upstream handoff checks: exact
accepted-opportunity snapshots, artifact lineage, evidence/page reference checks,
recommended-first-test linkage, and unchanged proposed/untested/unassessed statuses.
There were no repair calls, generation diagnostics, API errors or truncations.
Peak API concurrency was four. Process usage for `deepseek-flash`: 308603 prompt
tokens (24576 cached), 153693 completion tokens including 112426 reasoning tokens.

#### Content inspection and its limits

All 12 complete proposals were manually inspected against their accepted opportunity
scope; selected factual and numerical claims were also compared with the actual
original-page excerpts saved in the requests. All 12 provide tangible research
directions and mechanisms with suggested comparisons, observations and interpretable
outcomes. They stay at the requested suggestion level: no prescribed seed values,
repetition schedules, hardware allocation plans or resource estimates were identified.
Source-reported model/hardware/rank/sample-size conditions remain where they define
the question. Naming comparison alternatives or the factors relevant to an interaction
is allowed; this is not a return to a mandatory execution protocol.

The content review is **reviewed with refinement notes**, not a blanket scientific
or factual-fidelity pass. Examples of the recorded issues:

- **Hypothesis/test consistency:** MoE `dir-000` and KV `dir-003` have initial tests
  that probe only part of a compound hypothesis. RAG `dir-001` proposes separating
  noise filtering from conflict adjudication but does not directly compare that
  proposed method in its suggestions. RAG `dir-003` labels stable rankings as
  supporting in E4 even though its linked H4 predicts instability.
- **Success and falsification:** several candidates treat a robust or null result
  as refuting the whole research direction, although it could answer the original
  question while weakening only a proposed explanation. KV `dir-003` allows matching
  the best strategy in its expected effect but demands beating it in its falsification.
- **Inference strength and provenance:** KV `dir-000` predicts the direction of a
  change after matching compression ratios that is not established by the source
  comparison. KV `dir-002` inconsistently phrases an inferred absence as something
  the pages state. RAG `dir-002` assumes details of the baseline prompt interface
  that the supplied comparator pages do not establish. These assumptions must remain
  proposals or unknowns rather than become reported facts.
- **Meaning of the measurements:** KV `dir-003` needs to distinguish total workload
  decode time from individual-request latency; MoE `dir-003` needs to distinguish
  absolute time saved from relative speedup. RAG `dir-000` cannot reconstruct paired
  query outcomes from aggregate counts, and interval overlap alone does not establish
  the comparative conclusion its suggestion discusses.

The exact per-direction notes, including additional qualifications, are in
`/home/hema/research_runs/direction_v2_corpora_live/semantic_inspection.json`.
`summary.json` separates the successful operational checks from this content review.
Per-corpus `directions.json` files and raw calls are preserved unchanged. This is a
small manual inspection of generated candidates, not an exhaustive entailment audit,
a benchmark of future discovery, or an independent scientific judgment.

No implementation changes were needed to complete this run. The last full offline
suite remains 211 tests plus 13 subtests passed; it was not rerun for this
artifact/documentation-only follow-up. Proposed experiments were not executed,
and novelty search, candidate refinement and scientific critique remain unimplemented.
The next architecture stage remains novelty assessment; the recorded content issues
are concrete inputs for subsequent refinement and critique, not resolved findings.


### 2026-10-04 — Generator attribution and consistency correction

This corrects the recommendation to advance while basic generator mistakes were
still open. It does not add novelty search, a runtime scientific critic, detailed
protocols, or a requirement that every suggested test fully resolve its hypothesis.
A partial test is legitimate when its outcome interpretation states what it can
and cannot establish.

#### Diagnosis and exact module changes

- **`directions/generator.py`, `_source_context`:** the previous projection reduced
  each non-tension cross-paper condition to `c.text`, discarding its `hypothesis`
  flag. Conditions now retain `text`, `hypothesis`, and namespaced
  `selected_evidence_ids` restricted to the opportunity's existing citation set.
  Context evidence outside that set does not become citable. This fixes a concrete
  information-loss defect. All condition flags in the saved real-paper corpus are
  false, so this defect is **not established as the cause of those observed live
  mistakes**; synthetic true/false cases exercise the correction directly.
- **`directions/generator.py`, `SYSTEM`:** the earlier instructions already warned
  against unsupported author attribution and inconsistent outcomes. The remaining
  ambiguity was that accepted/validated upstream text could appear authoritative,
  “supporting” did not name the claim being supported, and whole-direction
  falsification could be interpreted as rejecting the usefulness of the question.
  The instructions now distinguish accepted questions from author statements,
  describe inferred absence as scoped to reviewed material, retain unknown prompt
  and implementation details as unknown, and explicitly relate outcome labels to
  the linked prediction. They distinguish measurement quantities and matching from
  beating a baseline, allow honestly described partial tests, and frame falsification
  around the proposed mechanism or benefit. A same-draft consistency check replaces
  any need for an additional review call. Prompt version is now
  `direction_v2_attribution_consistency`.
- **`directions/schemas.py`:** field descriptions for `GroundedRationale.statement`,
  `Hypothesis.falsification_criterion`, `Experiment.informative_outcomes`, and
  `DirectionDraft.what_would_falsify_it` now agree with those instructions. Fields,
  required values, reference validation and the `directions_v2` wire format are
  unchanged by this correction. These descriptions guide generation; they are not
  deterministic semantic validators.
- **`tests/test_direction_fidelity.py`:** two regression cases exercise actual
  request projection for findings and observations with both observed and
  hypothetical conditions and unselected context evidence. Four additional cases
  check that invalid live selections fail before client creation, and selected
  opportunities retain exact snapshots and parent lineage without modifying inputs.
- **`tools/validate_direction_generator.py`:** optional repeated
  `--opportunity CORPUS:ID` selectors and `--repeats` allow bounded repeated checks.
  All selections are loaded and validated before opening the client. The saved
  selection manifest records original mining hashes, selected IDs and the review
  criteria. Subset results reference the derived subset mining artifact; original
  saved inputs remain unchanged. Calls share the existing concurrency-four limit.
- **`tools/direction_validation_cases.py`:** adds predeclared manual regression
  criteria for the five selected real-paper opportunities, plus common attribution,
  hypothesis/outcome consistency, falsification and suggested-test scope checks.
  These are review criteria, not an automated scientific judge or substring test.

#### Validation procedure

The complete offline suite passes **217 tests plus 13 subtests**, with the existing
FAISS/NumPy deprecation warning. The six new cases test behavior and information
preservation; no mocked response is presented as proof of model reasoning quality.

The bounded live rerun uses these five previously affected opportunities, each
repeated twice, with unchanged hypothesis/test caps and a 20-attempt ceiling
(10 initial generations and at most one schema repair per generation):

```bash
uv run python -m tools.validate_direction_generator \
  --out /home/hema/research_runs/direction_v2_consistency_live \
  --opportunity moe:op-000-000 \
  --opportunity kv_cache:op-000-003 \
  --opportunity kv_cache:op-000-004 \
  --opportunity rag_grounding:op-000-002 \
  --opportunity rag_grounding:op-000-004 \
  --repeats 2 --max-api-calls 20
```

The baseline is `direction_v2_corpora_live/semantic_inspection.json`. The new run's
`selection.json` stores criteria before responses arrive. Attribution, outcome
polarity and broad-question falsification are assessed separately from scientific
merit or completeness of a research plan. The live results and inspection are
recorded below after reviewing every generated proposal in this bounded run.


#### First bounded rerun: operational success, content correction incomplete

`direction_v2_consistency_live` completed ten calls, with peak concurrency four,
no repairs, no diagnostics and no truncations. It produced ten directions,
39 hypotheses and 37 suggested tests. Usage: 241448 prompt tokens (102397 cached),
146336 completion tokens including 110491 reasoning tokens. All ten persisted
outputs passed operational checks.

The manual result is **not a clean consistency pass**. The high-sigma inferred-gap
attribution and the original reversed stable-ranking outcome label did not recur.
However, both prompt-comparator candidates still presumed that the original prompts
contained or omitted particular slots/exemplars not established by supplied pages.
Other outputs contained overlapping outcome interpretations, such as treating
sublinear growth as a prediction and any growth as contradictory, or conflating
uncertainty about a causal explanation with uncertainty about an observed prediction.
Per-output findings are preserved in that directory's `semantic_inspection.json`;
its `summary.json` explicitly distinguishes structural success from this review.

#### One bounded follow-up: explicit comparisons and primary predictions

The observed recurrence justified a focused formulation change, not another generic
instruction to “be consistent.” In **`directions/generator.py`**, the generator must
now construct variants with and without a feature when original baseline details
are unknown, and state that their relationship to the original implementation is
unknown. It must not describe removing a feature from a published baseline whose
contents were not supplied. Each hypothesis has one primary prediction; its
comparison and quantifier must stay consistent across expected effect, falsification
and outcome interpretation. A well-resolved outcome must not be both contradictory
and inconclusive; causal explanation can separately remain unresolved.

Prompt version is now **`direction_v2_explicit_comparisons`**, superseding the
intermediate prompt version above. Output shape, settings and suggestion-level
scope remain unchanged. The complete offline suite was rerun after this change:
**217 tests plus 13 subtests passed**. The follow-up uses the same five selectors
but `--repeats 1 --max-api-calls 10`, writing to
`/home/hema/research_runs/direction_v2_explicit_comparisons_live`. This permits five
generations and at most five schema repairs; it does not rerun upstream modules or
introduce a paid scientific judge. Both runs and their original responses remain
preserved; the final content result is recorded below.


#### Final follow-up result and current acceptance status

`direction_v2_explicit_comparisons_live` completed with five generated directions,
19 hypotheses and 19 suggested tests. Six API attempts were made: five generations
and one successful schema repair for an AttentionPack output whose later suggestion
was incorrectly marked `initial`. Peak concurrency was four; there were no final
diagnostics or truncations. Usage: 151148 prompt tokens (28544 cached), 82857
completion tokens including 60459 reasoning tokens. All five persisted outputs
passed contract and reference checks.

**The generator's content-correctness work is not closed.** Manual inspection of
all five complete outputs identified basic errors in two outputs:

| Output | Observed result |
|---|---|
| MoE `op-000-000` | Original broad-question falsification mistake not observed; mechanistic predictions and comparisons remain draft research proposals. |
| KV `op-000-003` | Missing high-sigma evaluation is correctly labeled inferred. However, a separate rationale confuses the widening Jensen gap with the shrinking evicted-output weight, and E2 gives equal degradation inconsistent contrary/unresolved interpretations. |
| KV `op-000-004` | Batch comparison explicitly fixes the query workload; original match-versus-beat contradiction not observed. Relative versus absolute advantage still needs clearer terminology. |
| RAG `op-000-002` | Tests now use explicitly constructed prompt variants with unknown relation to originals, but the mechanism still asserts an original prompt property categorically. H1 requires improvement on at least two models while E1 calls improvement on one inconclusive rather than contrary. |
| RAG `op-000-004` | Original polarity reversal not observed; primary reordering and its causal explanation are separated. A hypothesis repeats a contrast already implied by the two original benchmarks and needs sharper extension scope. |

The Jensen error was checked against actual supplied `436775470aa1#p7`: the text
says the gap widens and the approximate evicted-output weight shrinks. The candidate
incorrectly attributes shrinking estimator bias to the page. This is a grounding
error, not deferred scientific refinement. Likewise, contradicting an explicit
at-least-two threshold is a generator consistency error, not a requirement for a
detailed experimental setup.

The exact notes and distinction between basic errors and refinement issues are in
`direction_v2_explicit_comparisons_live/semantic_inspection.json`. Both live-run
summaries retain their operational success but include the separate manual failure
status. The original response JSON and repaired response are unchanged.

Across this correction task, 16 API attempts generated 15 candidate outputs in two
bounded runs. The final offline suite remains 217 tests plus 13 subtests passed;
no further code changes followed that run. The context-projection defect is fixed
and tested. The prompt/field-description changes improve attribution and clarify
comparison semantics, but the live evidence does **not** establish that they solve
all basic prose errors. Further prompt-only iterations were stopped rather than
claiming success, silently editing outputs, or introducing an unplanned runtime
critic. No novelty implementation was started. The earlier recommendation to move
on to novelty is superseded while these generator correctness issues remain open.


### 2026-10-04 — Independent correctness judge (`directions_v3`)

This changes the existing Direction Generator. It adds `directions/judge.py`
inside that module; it does not change Landscape Builder, Cross-Paper Reasoner or
Opportunity Miner. This entry supersedes the earlier one-call acceptance path and
statements that all prose consistency is deferred to downstream review. The
suggestion-level v2 proposal fields are retained: no detailed experiment protocols
are reintroduced.

#### Execution and responsibility

Every structurally valid generated candidate receives a separate model call with
its complete candidate, accepted opportunity, upstream evidence context and original
reviewed paper pages. The reviewer receives a fresh two-message conversation;
it receives neither generation chat history nor previous review verdicts. The
reviewer checks factual grounding and attribution, prediction/falsification/outcome
consistency, meaningful hypothesis/test links and accepted opportunity scope.
Explicitly partial tests and speculative mechanisms are allowed. The judge does
not assess novelty, scientific merit, feasibility, statistical power or execution
detail. Those responsibilities remain with the later architecture stages.

```
generate + reference checks → independent correctness review
                                ├─ pass → emit direction
                                ├─ abstain/error → retain diagnostic, withhold
                                └─ revise → one substantive revision + reference checks
                                              → fresh independent review
                                                  ├─ pass → emit revised direction
                                                  └─ otherwise → withhold
```

A revision has access to the first report and original evidence. The second judge
sees the revised candidate and original evidence, without the first report. The
revision cannot bypass existing schema or evidence-reference checks. There is no
unbounded revision loop and no switch that promotes an unreviewed candidate.
Review transport failures, exhausted budgets, truncated/malformed responses and
unresolved issues prevent that opportunity from producing an accepted direction.
Other independent opportunities continue unless the provider rejects the run.

Independence here means a distinct evaluation call and fresh context. By default
it uses the configured generation provider/model. Set `--review-model MODEL` or
`DIRECTION_REVIEW_MODEL` for a different reviewer model on that provider. Python
callers may inject `review_chat` independently. This does not establish statistical
independence or eliminate correlated mistakes when models share limitations.

#### Files changed and output contract

| File / component | Change relative to the implementation above |
| --- | --- |
| `directions/judge.py` (new) | Narrow correctness rubric, `direction_correctness_v2_full_candidate` review prompt, fresh-context request builder, issue pointer and source-quote validation. |
| `directions/generator.py` — `_review()`, `_complete()` | Mandatory review, at most one substantive revision, fresh re-review and diagnostics for withheld drafts. |
| `directions/generator.py` — `_generate()`, `_assign_ids()`, `_call()` | Revision requests carry actionable feedback; IDs are assigned before review; generation/revision/review share concurrency, input/output allowances and attempt budget. |
| `directions/schemas.py` | Adds `CorrectnessIssue`, `CorrectnessResponse`, `SourceSpan`, `ReviewRecord`; v3 results contain review records and each emitted direction links to its exact passing snapshot. |
| `tests/test_direction_judge.py` (new) | Review isolation, revision limits, abstention, malformed reports, genuine source quotes, unavailable reviewers, evidence validation after revision and persisted handoff integrity. |
| `tests/test_direction_generator.py` | Existing fixtures and call-budget expectations now include mandatory review. |
| `tools/validate_direction_generator.py`, `tools/validate_direction_fidelity.py` | Consume v3, record call tasks and allow bounded generation/review/revision attempts. Default total harness caps are 72 and 48 respectively; callers can set smaller caps. |
| `tools/validate_direction_judge.py` (new) | Bounded live positive/negative controls and saved real-draft review/revision/review checks; retains requests, responses, reports and final artifacts. |

A report is `pass`, `revise` or `abstain`. `pass` cannot contain issues; `revise`
requires actionable issues. Each issue identifies a real candidate field with a
JSON Pointer. Grounding/attribution issues require a verbatim source passage and
page ID; code checks membership against the supplied original page, normalizing
whitespace. This establishes quote provenance, not semantic entailment of the
judge's interpretation. Internal consistency issues need no paper quotation.

`GenerationResult.reviews` records each reviewed draft, report or error, model,
review prompt version and review round, including withheld candidates. Emitted
`Direction` objects carry `correctness_review=passed` and `correctness_review_id`.
The persisted v3 validator rejects a missing/failed/wrong-opportunity review, a
proposal differing from its reviewed snapshot, duplicate review IDs and bypass of
a later review. `scientific_review=not_assessed`,
`literature_novelty=not_assessed` and `hypothesis_status=proposed_untested` remain.
A correctness pass must never be presented as verified novelty or scientific truth.

`directions_v2` results are not silently upgraded or accepted as reviewed v3.
Existing historical artifacts remain unchanged. Regenerate to obtain reviewed
production output; the validation harness explicitly reuses selected saved drafts
only to test known failures and labels the skipped initial generation.

#### Budgets and concurrency

Unchanged production settings: concurrency 4, run attempt cap 40, and configured
input/output allowances (default 500,000 each). Each phase checks its complete
prompt without silently dropping evidence. Every request consumes the same atomic
attempt budget. A clean candidate normally takes two calls; revision followed by
re-review normally takes four. With the default one schema repair per generation
or revision, the per-opportunity maximum is six attempts. A malformed judge response
is retained as an error and withheld rather than triggering extra repair calls.
Independent opportunities execute concurrently; phases within one opportunity
follow their necessary dependencies. Actual API calls and categories are recorded.

#### Validation and remaining limits

The full offline suite passed **239 tests plus 13 subtests**, including 22 dedicated
judge tests. Stub tests establish control flow and contract integrity, not a model's
ability to judge research prose. Live results are recorded below after examining
the returned issues and revised proposals.

Even a passing independent judge can miss errors or raise false positives. This is
a bounded correctness filter, not a guarantee of a particular novel-hypothesis
percentage. Novelty search and later refinement/critique still need implementation;
the judge must not impersonate those stages or force a fixed number of outputs.


#### Live validation: controls, known failures and a reviewer correction

The authorized DeepSeek check used `deepseek-flash`, with peak concurrency four.
Artifacts are saved in
`/home/hema/research_runs/direction_independent_judge_live`. Twelve API attempts
included six isolated reviews and two complete saved-draft review/revision/review
flows (the two initial generations were explicitly reused, not counted as API
calls). Usage: 236,438 prompt tokens (106,240 cached), 137,996 completion tokens
including 126,274 reasoning tokens; no truncations.

The four fictional controls behaved as expected: the sound proposal and an
explicitly partial test passed; a reversed source quantity and reversed outcome
polarity were identified for revision. Real MOMENTKV review caught the Jensen-gap
misstatement with a genuine page-7 quotation. Real RAG review caught the
at-least-two-model threshold contradictions. Both drafts were revised once and
passed the original judge's second review.

**Manual inspection found those final passes incomplete.** MOMENTKV still labeled
equal low-to-high-sigma degradation both contrary and unresolved. RAG still made a
categorical assertion about an unknown original prompt inside its mechanism field.
The first run's `semantic_inspection.json` and annotated summary retain these
misses; original raw responses and output artifacts are preserved. The initial
live run establishes operational behavior, not clean content acceptance.

This evidence led to `direction_correctness_v2_full_candidate` in
`directions/judge.py`, superseding `direction_correctness_v1`. The rubric now
explicitly checks resolved equality/null interaction outcomes and every outcome
clause, and distinguishes a speculative causal explanation from an unconditional
claim about a published implementation inside the same mechanism paragraph.
It requires inspecting the complete candidate instead of stopping at the first
defect. No extra protocol requirements or runtime review calls were added.

The follow-up rechecks the saved revisions and the same four controls, making at
most six review calls and **no further revisions**:

```bash
uv run python -m tools.validate_direction_judge \
  --out /home/hema/research_runs/direction_independent_judge_followup \
  --review-only \
  --reviewed-drafts /home/hema/research_runs/direction_independent_judge_live \
  --max-api-calls 6
```

The complete offline suite after this reviewer change again passed 239 tests plus
13 subtests. Existing FAISS/NumPy deprecation warning only; `git diff --check` clean.


The follow-up completed six calls, peak concurrency four, without truncations.
Usage: 71,790 prompt tokens (2,048 cached), 65,803 completion tokens including
64,372 reasoning tokens. **Five of six expected decisions matched.** Both sound
controls still passed and both seeded errors were identified. The remaining
MOMENTKV equality contradiction was correctly identified for revision. The RAG
revision still passed despite the unestablished statement that the original
Chain-of-Note leaves the decision implicit. Its threshold inconsistencies were
fixed, but that attribution concern was missed. Source page 6 describes notes
before answering and page 8 reports zero refusals; neither establishes the precise
original prompt omitted a decision instruction. Details are preserved in the
follow-up `semantic_inspection.json` and summary. The harness exits nonzero for
this failed expectation; this is not relabeled as a clean semantic pass.

Across both runs, 18 actual API calls tested review and two bounded revision cycles.
The final production gate, audit contract and failure handling are implemented and
offline-tested. The known RAG reviewer miss remains a reliability limitation;
automatic correctness is not established. No further revisions were made, no
reviewer was repeatedly sampled until a preferred verdict appeared, and no
literature-wide novelty claim was introduced. These targeted, post-inspection
cases are not an estimate of accuracy on unseen research directions.

## 2026-10-05 — Explicit test-link audits and scoped revision support

This entry changes the correctness implementation above; it does not change the
scope of the generated directions into detailed experimental protocols.

- `directions/test_links.py` now defines the shared explicit audit for every
  hypothesis/experiment link: prediction, equality/null case, consistency decision,
  and reasoning. `directions/judge.py` asks for these checks, and
  `directions/generator.py` retains the full audit. A passing review cannot contain
  contradictory or uncertain links, omitted links, or duplicate links.
- `directions/schemas.py` validates that newly generated/revised directions match
  their exact passing audits. Existing `directions_v3` snapshots remain readable
  without changing their hashes or claiming they had this newer explicit audit.
- New `directions/revision.py` and `directions/revision_schemas.py` implement sparse
  edits to an existing direction. Inputs are the exact original direction,
  source-backed correctness requests, and only independently accepted scientific
  refinement requests. An independent original-page preflight confirms correctness
  concerns. Unaccepted critic suggestions cannot authorize scientific changes.
- Edits contain exact JSON Pointers, old/new values and issue IDs. Every accepted
  issue must be addressed, no other field may change, and hypothesis/experiment IDs,
  evidence references and links remain fixed. Source/page hashes are revalidated.
- Each revised proposal receives a fresh complete correctness review, including
  an explicit resolution of every requested correction and every test link.
  At most two patch attempts occur within a revision. An abstention, disagreement,
  source failure or unresolved issue withholds publication; passing requires the
  exact reviewed proposal. The original proposal and review history are preserved.

`critic/refinement_loop.py` orchestrates these revisions with novelty revalidation;
see [Research Critic](research_critic.md). It does not select a desired novelty
percentage or alter hypotheses merely to obtain a favorable verdict.
