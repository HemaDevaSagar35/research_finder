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

## 2026-10-05 — Bounded correction/refinement loop

This implements the correction path left open in the full live result above.
`critic/refinement_loop.py` adds `RefinementLoop.run(CriticResult)` and the
`refinement_loop_v1` artifact. It executes the accepted section-15 REFINE action
and located direction-correctness requests through the existing generator and
novelty stages. It does not implement final portfolio ranking or merge construction.

1. Independently confirm located correctness defects from the original pages.
   Scientific refinements enter only from a published, independently reviewed
   critique. Draft suggestions are not accepted instructions.
2. Apply field-scoped edits and independently review the entire revised proposal,
   including every hypothesis/test link and requested correction. Preserve IDs,
   source references and the original artifact.
3. Decide which novelty work still applies using exact scientific-field checks
   and independent full-evidence applicability review. Otherwise run fresh
   signature/search/comparison for the changed direction.
4. Rerun novelty synthesis and its independent review, then run a fresh Research
   Critic with the complete source packet. Do not transfer old final verdicts.
5. Repeat at most twice. Return `ready` only for complete ranking/stop handoffs;
   `needs_revision` preserves remaining work after the bound, and `blocked` records
   an execution failure, disagreement or unresolved correctness/evidence request.
   A bounded result does not assert that the research is sound or novel.

The optional `observations` input admits explicitly recorded manual correctness
concerns with source IDs and proposal fields. These are independently checked,
not treated as accepted facts or used to rewrite an earlier critic's verdict.
Requests for prior-paper evidence reassessment remain routed upstream; merge
construction and unrelated failed-stage retries are explicitly outside this loop.

`RefinementResult` retains original critic input, every sparse patch, full reviews,
revised generations, applicability certificates, any fresh search/comparison
artifacts and each fresh critic result. Reloading verifies these dependency links.
Independent directions run concurrently; dependent stages run in order. Budgets
are shared and no evidence is silently dropped to fit a provider context.

`tools/validate_refinement_loop.py` runs saved complete candidates, saves raw
requests/responses and stage checkpoints outside the repository, and supports
replaying only successful calls with identical messages/model/format. Output-token
reservation may differ on replay. The hybrid index loads lazily when new search
is actually required. Example:

```bash
uv run python -m tools.validate_refinement_loop \
  --critic /path/to/critic-result.json --out /path/to/new-run \
  --max-input-tokens 1000000 --max-output-tokens 65536
```

Automated and live validation results are recorded below after execution.

### Automated validation and first complete live attempt

`uv run pytest -q` passes **544 tests plus 13 subtests**. The 28 new loop tests
cover exact sparse edits, stable IDs, source quotations, independently passing
revisions, skipped/duplicate test links, unaccepted refinement rejection, reviewer
disagreement, artifact tampering, manual observations, reuse rejection, actual
fresh signature/retrieval/comparison execution, and the two-cycle bound. The
fresh-search test also verifies concurrent retrieval. The only suite warning is
the existing FAISS/NumPy deprecation warning.

`/home/hema/research_runs/refinement_kv_live_v1/` records the first complete live
attempt: **14 API calls**, two truncated responses recovered through the bounded
format-repair path, no source sampling and no checkpoint replays. H2/E2 was
corrected and independently passed; all five targets passed the full-evidence
applicability audit. Fresh novelty synthesis initially mixed the order-ablation
and sigma scopes, then corrected that wording and passed independent review.
The fresh critic identified the remaining H3 source-attribution issue, but its
request located `/rationale/4/statement`. The next correctness preflight interpreted
that rationale sentence as acceptable and missed E3's explicit low-sigma-anchor
wording. The loop stopped with reviewer disagreement; it did not publish a
scientific KEEP/REFINE decision or silently override either review.

This exposed two practical issues addressed in the recovery run:

- The exact E3 claim is now supplied as a recorded, source-located manual
  observation. Manual observations require independent confirmation and do not
  authorize wholesale application of the critic's unaccepted draft suggestions.
- Source transport now removes repeated per-passage envelope keys while preserving
  all original text, page grouping and reversible citations. The complete real
  packet round-trip preserved **7,782 passages** exactly, reducing the representative
  packet from roughly 3.82M to 3.63M characters (the older request also contained
  revision context). The live runner reserves 65,536 response tokens. Core input
  allowance remains 1M; evidence is never dropped to make the request fit.

The outer loop now also accumulates the nested critic's request-local usage. The
v1 raw ledger's 14 API calls is authoritative: its old outer `usage` field omitted
three nested critic calls and one of the two truncated responses. That historical
artifact is retained unchanged; later runs use the corrected accounting.

### Recovery run: proposal corrections passed; upstream interpretation exposed

`/home/hema/research_runs/refinement_kv_live_v2/` completed **nine live API calls**
with zero truncated responses or provider-context rejections. The explicit E3
observation let the independent preflight locate both defects. The sparse patch
changed only E2's `informative_outcomes` and E3's `why_this_test`; all scientific
hypothesis fields, IDs, evidence links and test links remained unchanged. A fresh
correctness review passed, and all five targets passed the independently reviewed
novelty-applicability check. This resolves the two known proposal defects, not
literature-wide novelty or scientific acceptance.

The complete fresh novelty synthesis/review then exposed an older **section-12
MOMENTKV/H3 interpretation error**. The corrected synthesis explicitly leaves the
order-ablation sigma unknown, but immutable upstream comparison dimension rationales,
relationship explanation and `/expected_effect/0` proposal-match explanation still
call the ablation a low-sigma operating point. The accepted individual claims c15
and c16 retain their scopes; the faulty inference joins those separate claims.
That sentence cannot be fixed by another synthesis or hypothesis edit.

The applicability judges missed this stale upstream interpretation. Their passing
reviews therefore do not guarantee that reused evidence is semantically flawless.
The downstream independent review caught it, although it initially assigned the
repair to synthesis instead of section 12. The candidate was withheld throughout.
No scientific KEEP/REFINE result is published for the corrected candidate, and the
old PARTIAL_OVERLAP findings are not presented as a newly accepted verdict.

Follow-up changes identify the owner explicitly:

- `novelty/assessment_prompts.py` now separates mutable assessment prose from
  immutable relationship/proposal-match/comparison-rationale fields. Wrong upstream
  interpretations require `reassessment_requests` even when individual claims are
  accurate. The review prompt version is `novelty_assessment_review_v6_ownership`.
- `critic/schemas.py::CriticResult.handoff` preserves upstream reassessment requests,
  novelty-review issues and the actual upstream diagnostic.
- `critic/refinement_loop.py` reports `upstream_evidence_reassessment_required` or
  `novelty_assessment_review_unresolved`, rather than losing that cause behind a
  generic missing revision route. It cannot repair prior-paper evidence by editing
  the candidate.

Repairing this saved prior-paper interpretation is separate from the implemented
candidate-refinement loop. It needs the existing evidence/relationship review and
comparison repair stages, followed by full novelty synthesis and scientific critique.
The loop and its tests are implemented; this real candidate is **not ready for ranking**.

### Final full-context ownership audit and remaining limits

`/home/hema/research_runs/refinement_kv_ownership_v3/` records **one additional
live API call**, independently reviewing the exact corrected v2 synthesis with
all prior evidence and the new ownership instructions. It did not publish an
assessment. It flagged missing explicit model/benchmark-unspecified qualifiers
in the direction, H1 and H4 synthesis prose, while finding H2/H3's qualifier prose
acceptable. It returned **no section-12 reassessment request**, despite the
independently located stale upstream H3 interpretation described above. Therefore
this audit does **not** establish that the model reliably routes every upstream
interpretation error. The deterministic route is tested when the model supplies
a valid request; semantic detection and ownership assignment remain imperfect.

Final validation: **545 repository tests plus 13 subtests pass**; **29** of those
tests cover the new loop. The last affected-stage run passed **165** tests before
the full-suite check. Across v1/v2/v3 there were **24 live API calls**, no checkpoint
replays, and two truncated responses (both v1; no truncation in v2/v3). All calls
have ended. Both complete loop artifacts reload under the final validators.

Remaining work for this saved candidate is concrete:

1. Correct and independently re-audit MOMENTKV/H3's section-12 relationship and
   comparison prose that calls the order ablation low-sigma. Preserve the accurate
   individual source claims and unaffected evidence; do not silently modify an
   accepted comparison artifact or its hashes.
2. Regenerate/review the full synthesis with explicit unspecified-model/benchmark
   qualifiers wherever the aggregate sigma measurements are restated.
3. Run the full Research Critic again on that newly accepted assessment. Only an
   independently accepted critique can authorize scientific refinement or ranking.

The new candidate-refinement implementation is complete and exercised live. The
saved candidate's scientific/novelty acceptance is **still withheld**. Neither the
raw PARTIAL_OVERLAP drafts nor a passing applicability certificate override that
remaining evidence and synthesis work.

### 2026-10-05 — Repair at the owning section-12 stage

The preceding unresolved H3 interpretation was reopened using new
`novelty/reassessment.py`, rather than changing the hypothesis to accommodate it.
The concern names MOMENTKV (`436775470aa1`), H3, and the original c15/c16 source
passages. Full source/evidence audits and a separate comparison review passed.
The repaired relationship and comparison say the order-ablation spread is
**unspecified**; the separate aggregate sigma analysis neither identifies its
model/benchmark nor establishes the ablation's sigma. The `PARTIAL_OVERLAP` label
remains unchanged. The correction changes its justification, not the outcome to
make the candidate pass.

Integrity verification of `momentkv_reassessment_v2/reassessment.json` retained
**all 100 target/paper comparisons across 54 papers**. Every other paper is exact;
within MOMENTKV only H3's relationship and pair changed. All 21 original claim
texts are exact and all 11 available pages are preserved. Fresh audits supplied
four additional known-page citations (c10, c11, c20, c21); these were attached and
independently re-reviewed. `integrity_check.json` records the exact changes.

The first attempt, `momentkv_reassessment_v1`, made four API calls and stopped
because the original scoped repair could not attach supporting citations newly
identified by the audit. It published no repair. The implementation now allows
only audit-justified citation additions with unchanged claim text and requires a
fresh review. The recovery replays exact successful patch/audit responses from
that attempt; later changed requests are fresh API calls. Raw logs and artifacts
are under `/home/hema/research_runs/`. Complete synthesis/critic outcomes follow.

The recovery's first synthesis and its independent reviewer both completed, but
manual inspection rejected the model's pass: all five targets omitted the
explicit unspecified model/benchmark qualifier when restating c15, and H1's
meaningful-difference prose linked budget-indexed margins to low sigma without
source support. The v2 run was stopped while its separate candidate-correction
review was in flight; its raw critique/correction drafts are historical evidence,
not the current accepted result. `manual_validation.json` records that disposition.

`momentkv_reassessment_v3` resumes the accepted section-12 repair and reopens only
the synthesis with the recorded observations. Its full revised synthesis and fresh
independent review passed. All five targets now explicitly carry the unknown
model/benchmark; the performance margins, order ablation and aggregate sigma
measurements stay separate. All five outcomes remain `PARTIAL_OVERLAP`, all four
hypotheses are retained, and every target has complete recorded comparison coverage.
The fresh Research Critic uses this corrected assessment. This confirms the
located wording repairs; it does not establish perfect semantic-judge recall.


### 2026-10-05 — Full scientific revision and recovery of the new shortlist

The accepted v3 critique located an H1 intervention inconsistency: comparing full
MOMENTKV with a renormalization-only baseline while requiring the same selection
method. The scoped correction now gives each arm its own selection protocol at
matched cache budget and context length. Independent correctness review passed.
Because this changes the intervention, the refinement loop required fresh
signatures, retrieval, per-paper comparisons and synthesis. It did not reuse the
old candidate's novelty verdict as acceptance of the revised candidate.

`momentkv_reassessment_v4` completed that full search: **100 target/paper pairs
across 50 distinct papers**, including every shortlisted paper for the direction
and four hypotheses. It made 311 fresh DeepSeek attempts and reused 15 exact
successful request/response checkpoints. Initially 82 pairs were accepted; 18
remained withheld across 11 papers (39 complete, 6 unresolved, 3 failed and 2
partial). Failures involved malformed responses, missing fields, wrong-paper
citations and unresolved interpretation/scope. There were no provider/network
errors or truncated responses in v4. Its synthesis was withheld for missing source
qualifiers, so its critic did not publish a scientific verdict.

The full regeneration also reintroduced the specific MOMENTKV/H3 mistake: joining
aggregate sigma measurements to the order ablation's unspecified spread. Manual
inspection caught this despite the general evidence audit. The scoped repair in
`momentkv_reassessment_v5` passed independent evidence and comparison reviews:
order-ablation spread remains unspecified, and the theoretical O(sigma²) term is
not presented as an observed monotonic performance effect. Generic model-review
recall is still imperfect; this live result must not be described as automatic
reliable detection of every scope error.

New `novelty/reassessment.py::IncompleteComparisonRecovery` continued the 11
incomplete papers concurrently with that H3 repair, retaining completed papers
exactly. V5 used 61 fresh calls and reached **98/100 accepted pairs, 48/50 complete
papers**. Its synthesis passed review but deferred the candidate for the remaining
H1 and H4 comparisons. V6 used 12 fresh calls and reached **99/100 accepted pairs,
49/50 complete papers**. One H4 audit had used the legitimate scope label `outcome`;
`novelty/comparison_records.py::ScopeCheck` now accepts that label without changing
citation or scientific-support requirements.

The last IceCache/H1 comparison in v6 remained withheld because successive audits
identified additional supporting citations. All claim decisions were supported
and the corrected target relationship was adequate, but the bounded repair rounds
ended with one citation still to attach and re-audit. Its earlier false claim that
IceCache lacks renormalization-only baselines was corrected: SnapKV and
StreamingLLM are included. Absence of a sigma-stratified comparison is scoped to
the reviewed material. Neither a citation-only blocker nor a passing synthesis
was used to bypass the final comparison review. V7 resumes only this paper before
rerunning the complete synthesis and critic; its final result is recorded below.

Call-accounting correction to the earlier v1 paragraph: its saved `calls.json`
contains **5 completed fresh calls**, not four. V2 contains 12 fresh attempts
(11 responses and one interrupted signature request) plus 2 local replays. V3
contains 29 fresh attempts (25 responses and 4 interrupted evidence requests).
Interrupted requests are attempts, not successful validations. Counts here refer
to the logged DeepSeek callback; embedding API requests are separate.

Current code validation: **565 tests plus 13 subtests pass**, with one existing
NumPy warning (`reassessment_final_regression_v3.log`). The new tests cover scoped
repair integrity, citation attachment, recovery lineage, fresh-review objections,
sparse comparison repairs, outcome-label validation, and source-guided assessment
reopening. All artifacts above are under `/home/hema/research_runs/`; source data
and credentials are not added to the repository.


V7 recovery completed **100/100 comparisons across 50/50 complete papers**. The
49 previously complete papers remained exact. IceCache c10 was clarified to
separate H2O's related-work description from the evaluated StreamingLLM/SnapKV
baselines; fresh evidence and comparison reviews passed. The complete synthesis
also passed: direction plus all four hypotheses are `PARTIAL_OVERLAP`, retained,
with all target coverage complete. The measured sigma values remain separate from
order-ablation and budget-indexed performance observations.

The first complete v7 Research Critic and its independent reviewer then published
an actionable scientific result: direction/H1/H3/H4 `REFINE`, H2 `KEEP`. They
accepted all four hypothesis/test links, including their equality cases. The
requested refinement assigns sigma strata using a pre-specified fixed reference
rule before comparing arms, and reports per-arm accuracy and sigma as well as the
margin. This avoids letting each evaluated method define its own comparison
groups. It is a scientific design clarification, not evidence that the predictions
are false or the candidate lacks novelty. The final allowed scientific refinement
cycle applies these requests, receives fresh correctness review, and revalidates
novelty because the conditions/test design changed. Its final disposition follows.


The v7 correction passed independent correctness review, but the subsequent
signature review withheld its new search for incorrect extracted-record
attributions and incomplete quotes. V7 ended after **17 fresh calls**, with its
initial accepted assessment/critique retained but the revised candidate's novelty
unresolved. Its `summary.json` reports `needs_revision`; the initial
`assessment_published: true` field must not be mistaken for acceptance of the final
revised candidate. There was no final section-15 KEEP for that revised candidate.

This exposed and fixed the direct-page provenance contract described in
[Novelty Search](novelty_search.md): original-page-supported facts need not invent
an extracted evidence ID. The v8 run resumes the saved signature and its actual
review feedback, preserving the scientifically corrected candidate. Its signature
revision and fresh independent review passed. Complete hybrid retrieval for the
direction and all four hypotheses then proceeded. V8 finishes the last scientific
cycle at the reviewed critic; it does not automatically start a third candidate
revision. This boundary does not limit the full novelty test.

After the signature fix, **575 repository tests plus 13 subtests passed**, with
one existing NumPy warning, in `reassessment_final_regression_v4.log`.


V8 completed the full revised-candidate shortlist: **100 target/paper pairs across
53 papers**, with 323 fresh DeepSeek calls. It saved 39 complete papers and 61
accepted pairs; 11 papers failed format/reference validation and 3 remained
unresolved. Failures included extra JSON data, misplaced/extra fields, empty claim
references in proposal matches and wrong-paper passage IDs. No provider/network
errors or truncations were reported. Its synthesis also failed reference
validation by citing unaccepted evidence; no assessment or scientific verdict was
published for that revised candidate. Raw results are retained in
`momentkv_reassessment_v8/`.

V9 recovers only those 14 incomplete papers and preserves the 39 completed papers.
A located MOMENTKV source concern is provided to the recovery evidence stage:
aggregate sigma observations, approximation-order ablation and budget-indexed
performance results must remain separate unless the original pages establish a
link. This is checked against the actual passages and is not authority to force a
novelty label. The first recovered draft now explicitly states that no measured
sigma is linked to the order ablation; it still requires independent evidence and
comparison reviews. Final coverage, synthesis and critique are recorded below.

Final code regression after source-concern recovery support: **581 tests plus 13
subtests pass**, with the same existing NumPy warning
(`reassessment_final_regression_v5.log`).


V9 finished with **89/100 accepted pairs and 48/53 complete papers**, after 102
fresh calls. Four MOMENTKV target comparisons passed with the source qualifiers
preserved; H1 still needed a newly identified supporting citation. The five
remaining papers comprised two malformed responses, two records with citation
attachments still required, and WILDCAT's comparison review falsely claiming that
wc-17 cited page 8: both its actual record and comparison draft already cited the
correct page-7 ID. That is a documented reviewer false positive, not a source
change. The synthesis again attempted to cite unaccepted evidence and was withheld
by deterministic validation. V9 therefore published no final revised-candidate
novelty or scientific verdict.

V10 continues only those five papers. Known prior-audit citations are attached
before a fresh audit, and the verified page-ID misreading is provided as located
feedback to check rather than authority to accept. The preceding 48 complete
papers remain unchanged. Every recovered target must still pass source and
comparison review before full synthesis and scientific critique.


V10 used **31 fresh calls** and reached **99/100 accepted comparisons, 52/53 complete
papers**. All MOMENTKV targets are accepted, including H1, with the source-scope
corrections preserved. The previously complete 48 papers are exact. The only
remaining paper, `0b15beb52116` for H4, passed its final claim/relationship audit but
still needed two source citations attached. Earlier audits corrected its overly
broad statement about residual scoring outperforming attention scoring across all
datasets/metrics; the record now distinguishes the actual per-dataset/per-metric
results. The reviewed synthesis was published with incomplete H4 coverage and
correctly routed to `resolve_coverage`, with no scientific verdict. V11 resumes
only that record, attaches the known citations before fresh audit, and reruns
complete synthesis and the Research Critic.

The final regression suite after the citation-attachment recovery improvement is
**582 tests plus 13 subtests passed**, with one existing NumPy warning
(`reassessment_final_regression_v6.log`). `git diff --check` also passes.


V11 used **12 fresh calls** and completed **100/100 comparisons across all 53
papers**, with the prior 52 complete papers exact and artifact lineage validated.
The final paper also needed a dataset-specific correction: its 100-frame
matching/surpassing statement applies to 7-Scenes, not NRGBD. The corrected source
record and comparison passed independent review. No skipped or withheld
comparisons remain in `momentkv_reassessment_v11/recovery.json`.

Its synthesis still withheld publication: it cited c17 under the direction-level
MOMENTKV evidence even though that claim was available under H1. The initial draft
also pooled three direction-specific IDs into H1; its first repair fixed those
but missed c17. The validator's generic error has been replaced by exact per-entry
reference diagnostics; the evidence gate itself is unchanged. V12 reuses the
validated complete comparison checkpoint and reruns only synthesis and critique.
Automatic approval initially rejected that retry as possibly broader data sharing.
A local equality/hash check proved its scientific packet and observations identical
to v11's already transmitted DeepSeek request; the same command was then approved.
No additional papers, source pages or provider were introduced by that retry.


V12's new synthesis passed citation validation on its first response. Its first
independent review requested two synthesis-owned citation corrections: separate
the order and component ablation references, and cite/remove an adjacent-work
metric. Those changes were applied, but the second review identified unsupported
averaging detail ("over heads and layers") and an overbroad head/layer attribution
to adjacent ReST-KV evidence. The partial-overlap findings and retain-all action
were considered defensible, but the synthesis remained unpublished. V13 resumes
that exact reviewed draft and its correction report using the same complete
comparison input, rather than regenerating source evidence or copying a pass.


### 2026-10-06 — Final full-run result for the second scientific correction

V13 finished with **7 fresh DeepSeek calls**, no replayed responses, no provider
errors or truncation. It reused the validated v11 comparison checkpoint without
rerunning retrieval or discarding any comparisons: **100/100 accepted target/paper
comparisons across 53/53 complete papers**. The full sections-13/14 synthesis passed
independent review after its remaining H1 model/benchmark/budget qualifiers were
restored. The direction and H1–H4 all receive `PARTIAL_OVERLAP`; all four hypotheses
are retained. This is a positive, scoped novelty outcome for the retrieved corpus,
not a claim of worldwide novelty or empirical scientific validation.

The section-15 critic ran, including structural repair and independent review.
Its draft recommended KEEP for the direction and H1–H3, and REFINE for H4, but the
independent review did **not** accept the complete critique. Consequently these
are draft recommendations, not published scientific actions. The final handoff
is `resolve_upstream`, with `scientific_action: null` and a `direction_correctness`
request for H4/E4. The runner completed successfully; the candidate is not yet an
approved section-16 output. `portfolio_complete: true` in this single-candidate
handoff does not override that blocked candidate route.

The remaining proposal issue is concrete: H4 defines fixed reference-sigma groups
while both naming attention-only selection as a possible reference and describing
the reference as independent of the compared scoring rules. E4 then treats low
attention-only sigma inside the high-reference-sigma group as an unresolved
contrast. This needs an unambiguous reference definition and matching outcome
clauses. Separately, smaller *relative* sigma reduction alone does not establish
a weakening suppression mechanism: baseline sigma and absolute reduction are
needed to distinguish that interpretation from a denominator effect. These are
high-level hypothesis/measurement consistency issues, not a demand for a detailed
experimental setup.

The review itself also needs scrutiny. Its blanket statement that an independent
high-reference-sigma group cannot contain low attention-only sigma is not valid
when the reference is a genuinely different fixed rule; those two measurements
can differ. The original ambiguity is real, but that particular reviewer argument
must not be treated as a proven contradiction. The critique also overstated the
prior's reporting as only budget averages, overlooking across-layer values. Its
H3 contribution sentence again called the prior ablation a low-spread operating
point, although the accepted evidence explicitly leaves that ablation's sigma
unreported; the independent reviewer did not flag that sentence. Publication was
withheld for the issues it did identify. This run therefore demonstrates both
working conservative routing and remaining semantic reviewer false positives and
misses; it does not establish that two model judgments guarantee correctness.

Two scientific candidate corrections have already been exercised. V13 completes
the final corrected candidate's full comparison/synthesis/critic checks using
`--skip-refinement`; it does not silently start a third scientific correction or
reduce the shortlist. Further candidate work should first resolve H4's reference
and effect interpretation, then re-run correctness and any affected novelty checks
before a fresh critic. The accepted section-12 and section-13/14 artifacts remain
available and must not be confused with an approved scientific verdict.

Final artifacts under `/home/hema/research_runs/momentkv_reassessment_v13/`:
`recovery.json` (validated lineage and complete comparisons), `assessment.json`
(accepted synthesis), `critic.json` (withheld critique and actual review),
`summary.json` (final routing), and `calls/` (all seven request/response records).
All three typed result artifacts reload successfully. Final regression:
**589 tests plus 13 subtests passed**, with one existing faiss/NumPy deprecation
warning (`/home/hema/research_runs/reassessment_final_regression_v8.log`).
