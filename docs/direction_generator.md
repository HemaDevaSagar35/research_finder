# Direction Generator (`directions/`)

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
