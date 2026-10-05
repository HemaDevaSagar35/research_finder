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

### 2026-10-05 — Authorized full live run and consistency-audit correction

The user explicitly authorized retrying the complete payload after the approval
question. Implementation was committed first as `f6c9074`; the external retry was
then allowed. Every run below used the complete saved MOMENTKV direction, all four
hypotheses, 100 accepted comparisons, and original supporting context.

- `research_critic_kv_live_v1`: author completed; reviewer request exceeded the
  provider's 1,048,576-token context by 3,117 tokens because 32,768 output tokens
  were reserved. No review or scientific acceptance was fabricated.
- `research_critic_kv_live_v2`: reused only the successful identical author request
  and reduced reserved output to 16,384. The complete-context independent reviewer
  passed KEEP for the direction and all four hypotheses. **Manual inspection
  rejected that pass for downstream use**, as recorded in `manual_findings.json`.
- The concrete miss is in the saved H2 experiment: equal low-to-high changes falsify
  its positive interaction prediction, but a later clause also calls a shared offset
  with unchanged low-to-high changes unresolved. The critic missed this contradiction.
  Its negative-result prose also conflated absence of a protective interaction with
  absence of any protective main effect. The saved H3 test rationale calls the order
  comparison a low-sigma anchor even though the accepted evidence separates the order
  ablation from sigma measurements. These are candidate/reasoning defects, not proof
  that the scientific questions are worthless or already studied.

The implementation now emits `research_critic_v2`, while retaining v1 readability.
`critic/schemas.py` adds a per-hypothesis/experiment `TestLinkCheck` recording the
prediction, equality case, consistency decision and reasoning. A new v2 result
cannot publish pass with missing, duplicate, unknown, contradictory or uncertain
link checks. `critic/evidence.py` enforces exact link coverage and requires a located
upstream direction-correctness request for a detected contradiction.

Internal consistency requests now identify candidate `field_paths` with
`basis=proposal_consistency`; no fabricated paper citation is required. Original
source-grounding requests still require source references. Prompts explicitly check
all outcome clauses, distinguish zero interaction from missing evidence, distinguish
main effects from interactions, and avoid treating an O(sigma^2) bound as guaranteed
monotonic error growth or borrowing the sigma setting of another experiment.

`tools/validate_research_critic.py` additionally supports explicit checkpoint replay
of successful, untruncated requests with identical messages/model/response format.
Only output reservation may differ. Replays and actual API attempts are reported
separately, and raw provenance is retained. This avoids regenerating an already
completed draft when only a provider output reservation needs adjustment.

Regression suite after the correction: **274 passed** (51 critic + 223 novelty).
The full-context v3 rerun reached the 16,384 output limit in both its draft and bounded
repair; both were rejected as truncated. A v4 rerun reserves 24,576 output tokens,
retaining all original evidence. Its final outcome is recorded below when complete.

### Final full-context outcome and recovery — 2026-10-05

The final validated result is
`/home/hema/research_runs/research_critic_kv_live_v6/result.json`, with explicit
integrity checks in the adjacent `integrity_checks.json`. Its independent review
flags **H2/E2's contradictory equality/null interpretation** and requests
`direction_correctness` for `/hypotheses/1/falsification_criterion` and
`/experiments/1/informative_outcomes`. The code-owned handoff is `resolve_upstream`.
**No scientific action is published and this candidate is not ready for ranking.**
This is an actual detected proposal defect, not an API permission block, missing
novelty comparison, or a conclusion that the hypotheses lack novelty/value.

The upstream assessment, generation and scientific source packet are unchanged:
all five targets, 100 comparisons and 7,782 passages were supplied. All four
hypothesis/experiment links were independently checked. No automatic rewrite was
applied. The draft's other refinement suggestions remain unaccepted model judgments,
not an authoritative instruction to redesign all four hypotheses. In particular,
any later stratification revision should distinguish the need for a clear shared
reference from a stronger claim that a reference based on either compared method
must always be forbidden; that stronger claim needs justification.

Follow-up implementation changes from the live runs:

- `critic/pipeline.py` includes the exact valid proposal-field inventory in prompts.
- `critic/evidence.py` allows an experiment revision to name a linked hypothesis
  or its direction. It still rejects unrelated hypotheses and nonexistent fields.
- `critic/schemas.py` derives recheck dependencies from the experiment's actual
  hypothesis links, so a shared-test edit rechecks every affected hypothesis.
- `normalize_review_paths` corrects only the exact unambiguous misplaced collection
  `/targets/N/revisions` to `/revisions`, and only if that existing target actually
  has revisions. It never alters review reasoning, decisions, requests or citations.
  Other invalid paths still fail. Normalizations are recorded in run metadata.

The v4 full author/repair calls completed but failed the then-overly-restrictive
revision path contract. The v5 author completed with valid field paths; the fresh
full-context reviewer correctly detected H2's contradiction. Its issue addressed
`/targets/2/revisions` rather than the draft's flat `/revisions` collection. A model
format-repair request exceeded the provider context by **41 tokens**. After the
strict structural normalizer was tested, v6 replayed the two completed, identical
v5 author/reviewer calls locally: **zero additional API calls**. Saved report
comparison verifies that only this one recorded pointer changed (besides reversible
source-alias restoration); the scientific review is exactly the original live one.

| Run | API attempts | Local checkpoint replays | Outcome |
|---|---:|---:|---|
| v1 | 2 | 0 | Draft complete; review rejected for context reservation. |
| v2 | 1 | 1 | Automated KEEP pass, manually rejected for missed consistency/scope defects. |
| v3 | 2 | 0 | Draft and bounded repair truncated; neither accepted. |
| v4 | 2 | 0 | Complete responses; revision-pointer contract failed. |
| v5 | 3 | 0 | Full author/reviewer complete; review detects H2 contradiction; pointer repair request rejected for context reservation. |
| v6 | 0 | 2 | Exact completed-call replay plus structural pointer normalization; valid upstream-correction handoff. |

Total: **10 API attempts** (two provider context rejections; two responses truncated)
and **three separately recorded local checkpoint replays**. All requests have ended.
Final relevant validation: **54 critic tests pass**, alongside the **223 passing
novelty regression tests**. The last combined run before the additional pure
pointer-normalization test passed 276 checks; the final critic rerun passed all 54.
The CLI deliberately returns nonzero for a withheld candidate, even when the model
review and its upstream-correction routing completed correctly.

This closes the requested full live critic execution, not scientific approval of
this candidate. The confirmed next correction is in the saved generated proposal's
H2/E2 outcome wording. Correctness and any affected novelty/scope checks must be
resolved on the revised artifact before a fresh critic decision can advance it.
