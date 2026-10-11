# Stage 10 — `assessment`

Design reference: architecture §13 Direction-Level vs Hypothesis-Level Novelty and §14 Novelty Outcomes. Implementation notes: `docs/novelty_search.md`.

## Aim

Combine the accepted pairwise comparisons into one novelty finding per target
(each hypothesis, then the direction), decide what to do with the direction
(retain / narrow / reframe / reject / defer), and hand the result to the
critic. Prior evidence is immutable here: the assessor may only cite accepted
claims and passages, and a reviewer checks every judgment.

## Inputs

Entry point: `NoveltyAssessor(chat=None, *, review_chat, provider, model, review_model, settings).run(generation, search, comparisons, *, invalidations=None, recovery=None, previous=None, observations=None, guidance=None) -> NoveltyAssessmentResult`
— class `novelty/assessment.py:26`, `run` at `:117`. Pipeline:
`NoveltyAssessor(settings=c.assessment, **common).run(state['directions'], state['novelty_search'], state['comparison'])`
(`research/pipeline.py:180`).

| Input | Contract |
|---|---|
| `generation`, `search`, `comparisons` | Stages 7–9; full lineage and shortlist/signature/hash consistency enforced by `AssessmentInputs` (`novelty/assessment_schemas.py:27`). |
| `settings: AssessmentSettings` (`assessment.py:21`, extends comparison `Settings`) | `comparison_coverage_threshold` **0.8** (the 60% replay used 0.6 via `recovery_manifest.json`); `individual_assessments` **True** → contract `isolated_v1`. |
| `invalidations` | Papers whose evidence must be treated as `invalidated` (refinement loop). |
| `recovery`, `previous`, `observations`, `guidance` | Reuse / reopen hooks used by `research/recover.py` and the refinement loop. |

Finding vocabulary (`TargetAssessment.finding`, `assessment_schemas.py:103`):
`ALREADY_STUDIED`, `PARTIAL_OVERLAP`, `COMPONENTS_KNOWN`, `LOW_PRIOR_OVERLAP`,
`UNRESOLVED`. Refinement (`:117`): `action`
`retain|narrow|reframe|reject|defer`, `retained_hypothesis_ids`,
`removed_hypothesis_ids`, `scientific_edits` (JSON-pointer edits to the
proposal).

Prompt versions (`novelty/assessment_prompts.py`): `VERSION =
'novelty_assessment_v7_1_isolated_targets_pending_subset'`, `REVIEW_VERSION =
'novelty_assessment_review_v8_isolated_targets'`; prompts `HYPOTHESIS`,
`HYPOTHESIS_REVIEW`, `SYNTHESIS`, `LIFECYCLE`, `ASSESS`, `REVIEW`.

## Logic

1. `build_packet(inputs, direction_id)` (`novelty/assessment_evidence.py:13`): for each target × shortlisted paper, classify the pair `reviewed` (accepted comparison, relationship not `insufficient_evidence`), `withheld`, `skipped`, `failed`, or `invalidated`. **Coverage gate:** if `reviewed / total < threshold` the target gets `coverage.complete=False` with `blockers` listing each non-reviewed paper. Evidence rows (claims + passages) are collected for reviewed pairs only.
2. With `individual_assessments` (the default):
   1. Per hypothesis: `hypothesis_packet` → `scope_sources` (`assessment_evidence.py:235`) limits evidence rows to that target and passages to cited + same-page passages; `_candidate` (`assessment.py:60`) runs `assess_novelty` with `HYPOTHESIS` → `TargetAssessment`, then `review_novelty_assessment` with `HYPOTHESIS_REVIEW`; `hypothesis_draft` (`:280`) wraps the judgment into a single-target draft whose refinement action is derived from the finding.
   2. `locked_targets` (`:289`): accepted child judgments become `locked_hypotheses`; failed ones are forced `UNRESOLVED`.
   3. `synthesis_packet` (`:262`) replaces full pages with cited passages only; the direction `_candidate` runs `SYNTHESIS` + `LIFECYCLE` → `DirectionSynthesis`; code composes the `AssessmentDraft` by inserting the locked hypotheses (they cannot be changed by the synthesis).
3. `_candidate` review loop (≤2 rounds): `_validated_call` (`assessment.py:35`) aliases passages to `srcN`, restores them, runs `validate_draft` / `validate_report` (`assessment_evidence.py:121`, `:219`), one format repair; `pass` publishes, `revise` gives one revision round, `abstain` or `reassessment_requests` withholds with a diagnostic.
4. `validate_draft` rules of note: `ALREADY_STUDIED` requires a direct same-target relationship; `LOW_PRIOR_OVERLAP` requires complete coverage; `retain`/`narrow` may not keep an `ALREADY_STUDIED` hypothesis; retaining `UNRESOLVED`/below-coverage hypotheses without edits is allowed (they stay pending on their own judgment).
5. `refinement_handoff(direction)` (`assessment_schemas.py:217`) computes what happens next: `requires_novelty_recheck_target_ids` (hypotheses whose text the `scientific_edits` touch; direction-level edits mark retained hypotheses only when not isolated), `unresolved_target_ids`, and `next_stage` ∈ `research_critic | direction_revision_then_signature_search_comparison | narrow_direction_and_recheck_scope | resolve_coverage | stop`. `selection_scope` is set when isolated, action `retain|narrow`, and no edits (`assessment.py:191`).

## Outputs

`NoveltyAssessmentResult` — `novelty/assessment_schemas.py:247` (`schema_version='novelty_assessment_v1'`).

| Field | Contract |
|---|---|
| `inputs`, `inputs_sha256` | Full `AssessmentInputs` and digest. |
| `candidates: list[CandidateAssessment]` | Per direction: `direction_id`, `candidate_sha256`, `coverage: CoverageRow[]` (per target, `retrieval_status`, per-paper `status` + `reasons`), `assessment: AssessmentDraft \| None` (`targets: TargetAssessment[]`, `refinement`), `reviews`, `format_repairs`, `diagnostic`, `hypothesis_assessments` (the per-hypothesis `CandidateAssessment`s). |
| `calls`, `usage`, `run` | Budget, usage, prompt versions, settings, `source_transport='page_grouped_source_map_v2'`. |
| `literature_wide_novelty='unverified'`, `scientific_quality='not_assessed'` | Explicit limits. |

`TargetAssessment`: `target_id`, `finding`, `reasoning`, `evidence` (accepted
claim/passage refs), `meaningful_difference`.

Persisted as `out/assessment.json`.

## What later stages rely on

- `critic/evidence.upstream_route` blocks the direction critic unless `refinement_handoff.next_stage == 'research_critic'` with empty `unresolved_target_ids` and `requires_novelty_recheck_target_ids`.
- `critic/hypotheses.eligibility` blocks a hypothesis review when its coverage is incomplete (`Incomplete novelty evidence: ...`), it is in `requires_novelty_recheck_target_ids` (`Proposed scientific changes require a fresh novelty check.`), or it is in `removed_hypothesis_ids`.
- `hypothesis_reviews.assessment_sha256 = digest(assessment)` ties stage 16 to this exact result.

## Implementation vs design

§13/§14 are implemented, with the `isolated_v1` contract added: hypotheses are
judged first in isolation and locked before the direction is synthesised, so
a direction-level revision cannot silently change a hypothesis verdict. The
coverage threshold is a code-level gate not present in the design.
