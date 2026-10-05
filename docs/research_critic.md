# Research Critic (`critic/`)

## Purpose and architecture boundary

Implements section 15 of `research_path_generator_architecture_refined.md` after
accepted sections 13–14 novelty refinement. It evaluates whether a surviving
scientific question is worth pursuing, separately from evidence correctness and
prior-work overlap. It does not prove novelty, validate predictions experimentally,
produce novelty percentages, require a fixed acceptance rate, or perform final
portfolio ranking (sections 16–17).

A plausible, untested mechanism may survive. A resolved negative result may still
answer a valuable research question. High-level discriminating tests are sufficient;
the critic does not demand execution protocols, hardware choices, seeds or sample
sizes. Source-qualified facts must remain distinct from proposed explanations.

## Input and source context

`ResearchCritic.run(NoveltyAssessmentResult)` receives immutable generation,
retrieval, comparison and assessment snapshots. Only the existing
`refinement_handoff` route to `research_critic`, with no unresolved/recheck targets,
is eligible. Rejected, deferred, narrowed or scientifically edited upstream
candidates remain recorded with their upstream route; they cannot silently acquire
an accepted scientific judgment.

Each eligible direction receives its full proposal, opportunity, all original
hypotheses/test links, accepted novelty assessment, coverage ledger, all accepted
comparisons and their available original passages. The critic additionally loads
the original opportunity's supporting `paper.json` artifacts and context pages via
`PaperStore`, verifying the generator's hashes and evidence references. This closes
the gap in the novelty synthesis packet, which did not explicitly carry the full
original opportunity context. No appendix extraction or full-PDF completeness is
assumed. Local/S3 source adapters remain reusable; the reasoning core accepts an
injected store and is independent of the CLI or developer filesystem.

Source transport preserves all scientific text. Original source IDs become
reversible local aliases. Repeated accepted claims are grouped by paper/claim ID;
comparison rows retain references to the complete claims. Support pages already
present in the source packet are represented by references in the support manifest.
Author and reviewer receive the same full evidence. Context exceeding an allowance
is reported, never silently sampled or interpreted as a scientific rejection.

## Scientific decisions

Every direction and hypothesis has exactly one finding for each architecture question:

- `gap_support`
- `meaningful_distinction`
- `combination_justification`
- `mechanism_plausibility`
- `testability`
- `falsifiability`
- `negative_result_value`
- `research_contribution`

Findings contain reasoning, original source references, proposal field paths and
an explicit basis (`source_fact`, `scientific_inference`, `proposal_analysis`).
Assessments are `adequate`, `concern`, `uncertain`; `not_applicable` is permitted
only for component combination. Source facts require source references; existence
checks cannot establish semantic support, which remains a review responsibility.

Individual actions are `KEEP`, `REFINE`, `DOWNRANK`, `DISCARD`. They include a
rationale, learning from a negative result and recommended next step. Discard
requires an identified concern. A weak hypothesis need not discard its direction.
There are no numerical scientific quality or novelty probabilities.

`REFINE` requires explicit revision requests addressed to existing scientific
fields. IDs, evidence and rationale are not edited. The module stores proposed
changes, never silently applies them. A shared scientific edit requires rechecking
the direction and surviving hypotheses; a hypothesis edit requires rechecking that
hypothesis and direction. Removing a hypothesis preserves the other hypotheses but
requires a narrowed direction-scope check. Correctness and novelty must be checked
on the actual revised version before it can advance. The automatic revision executor
is outside this module: its required work is explicit in the handoff.

Processing failures, source loading errors and abstentions withhold critique and
record diagnostics. They do not become `DISCARD` or evidence of scientific weakness.
Source defects discovered in accepted upstream material produce explicit requests
for direction correctness, section-12 comparison, or section-13/14 reassessment.

## Independent review and corrections

A separate fresh LLM conversation checks unsupported praise and unsupported criticism,
all targets, source attribution and experimental scope. It also checks whether the
suggested revisions are justified rather than demands for already-proven predictions
or unnecessary experimental detail. A separately configured reviewer model/client is
supported; using the same model in separate calls is independent conversation review,
not an independent empirical oracle.

The reviewer returns `pass`, `revise` or `abstain`, explicit checks for every target,
and located actionable issues. At most one semantic correction follows, with a fresh
review. Unaffected target decisions and revision requests must remain exactly intact.
Malformed output has one format-repair attempt per call. Source reassessment requests
stop publication; subsequent critique cannot conceal them. Saved results validate
exact proposal hashes, source packets, review histories and the final accepted draft.

## Cross-candidate merging and downstream handoff

Eligible accepted directions are considered jointly after concurrent per-direction
critique. A merge requires a shared scientific question, not topic similarity. Each
`MERGE` proposal identifies all member directions and preserves every constituent
hypothesis by `(direction_id, hypothesis_id)`, explains the shared question and
scientific distinctions, and cites original evidence. Groups must be disjoint.
A fresh reviewer audits the merge assessment. Zero or one eligible direction needs
no model call because no merge is possible.

Merged candidates require revision, correctness and fresh novelty checks; old novelty
labels cannot certify a changed combined proposal. Merge proposals are not applied by
this module. Directions requiring refinement or scope narrowing are not included in
the merge pass. An incomplete portfolio review blocks an otherwise eligible candidate's
ranking handoff. Ranking itself remains downstream.

## Implementation files and existing-module changes

- `critic/schemas.py`: strict scientific findings, revision/reassessment requests,
  review history, merge plans, immutable result and code-owned downstream routes.
- `critic/evidence.py`: input eligibility, original support checks, complete packets,
  references/pointers, action invariants, merge membership and saved-result validation.
- `critic/prompts.py`: scientific critique, independent audit and joint merge prompts.
- `critic/pipeline.py`: asynchronous candidate processing, source loading, lossless
  transport, format repair, bounded corrections and reviewed portfolio processing.
- `critic/__main__.py`: development CLI; the core remains transport-independent.
- Existing `novelty/pipeline.py`: adds critic/review-repair task names to reviewer
  model/client routing; existing novelty behavior is unchanged.
- Existing `llm_client/usage.py`: adds a context-local usage scope, inherited by async
  child tasks, while retaining existing global accounting. Critic runs use this to
  keep usage totals separate across concurrent service requests.
- `tests/test_research_critic.py`: deterministic behavior and failure-boundary tests.
- `tools/validate_research_critic.py`: full saved-input live validation, raw call
  artifacts, immutable result reload, summaries and a no-call preparation mode.

Independent candidates and source reads run concurrently; generation then review,
correction then re-review, and joint merging after individual critiques retain their
necessary dependencies. The existing shared semaphore and atomic attempted-call budget
bound requests. Settings reuse novelty allowances, including the configured 1M input
allowance; provider limits remain independent. A CLI output path cannot overwrite an
existing result. Each run requires a new `ResearchCritic` instance.

## Usage

```bash
uv run python -m critic \
  --assessment /path/to/novelty-assessment/result.json \
  --root /path/to/markdown \
  --provider deepseek \
  --max-input-tokens 1000000 --max-output-tokens 32768 \
  --out /path/to/new-critic-result.json
```

For live validation, use `uv run python -m tools.validate_research_critic` with
`--assessment`, `--root` and a new `--out` directory. Add `--prepare-only` to check
source integrity and complete packet size without API calls. Raw requests/responses
and scientific evidence are saved under the requested run directory, not in Git.

## Validation history — 2026-10-04

Initial critic suite: **34 passed**. Critic plus existing novelty regression suite:
**257 passed**. Coverage includes all-target/all-criterion checks, source hashing,
source alias integrity, invalid references, reviewed-result tampering, independent
reviewer routing, abstention/provider/budget/source failures, revisions and novelty
rechecks, partial hypothesis removal, unaffected-target preservation, merge membership
and concurrent candidate execution.

Full MOMENTKV preparation succeeded with **five targets (direction plus four
hypotheses), 100 accepted comparisons and 7,782 original source passages**. The
complete transported packet is approximately 949,955 input tokens by the local
estimator, before prompt/schema/draft overhead. No scientific text was sampled.

The first live launch was rejected before execution by automatic approval review,
which classified the complete critic evidence payload as broader than earlier API
authorizations. Specific user authorization is pending. Preparation and deterministic
tests are not a completed live scientific critique. Live results and any further
validation will be appended below rather than replacing this execution history.

### Extended contract validation

The complete deterministic regression run finished with **266 passed** (43 critic
checks plus 223 existing novelty checks). Added end-to-end two-candidate tests verify
concurrent individual critiques, accepted joint merge review, immutable artifact
reload and mandatory correctness/novelty rechecks for all merged hypotheses.
Portfolio-review abstention preserves candidate critiques while blocking ranking.
Additional checks cover request-local token accounting, fresh reviewer alias mapping,
upstream-ineligible candidates, single-use instances, DOWNRANK routing and preservation
of unaffected judgments when reloading a corrected result.

The handoff also preserves surviving experiment-to-hypothesis links. Merge citations
must belong to member directions and provide support for each member. Complete live
MOMENTKV validation remains **not run**: the automatic-review rejection happened before
process execution. The prepared live validator is ready to resume after the specific
payload authorization. No empirical or live-model acceptance is claimed from fixture
tests. The independent review mechanism remains fallible; source-reference validation
checks identity, not the truth or scientific value of reasoning.
