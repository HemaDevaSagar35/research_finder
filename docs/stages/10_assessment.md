# Stage 10 — `assessment`

Design reference: architecture §13 Direction-Level vs Hypothesis-Level Novelty and §14 Novelty Outcomes. Implementation notes: `docs/novelty_search.md`.

## Aim

Stage 9 produced up to thirty pairwise relationships per hypothesis. This
stage reads them together and answers, per hypothesis and then for the
direction: *given everything the prior work shows, is this still worth
proposing, and in what form?* Each hypothesis receives one of five findings
(`ALREADY_STUDIED`, `PARTIAL_OVERLAP`, `COMPONENTS_KNOWN`, `LOW_PRIOR_OVERLAP`,
`UNRESOLVED`); the direction receives a plan (`retain`, `narrow`, `reframe`,
`reject`, `defer`) that says which hypotheses survive and whether the
proposal text must change. The stage may only cite the audited claims from
stage 9 — it cannot introduce new prior work — and every judgment is checked
by an independent review.

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

Prompt versions (`novelty/assessment_prompts.py`): `VERSION =
'novelty_assessment_v7_1_isolated_targets_pending_subset'`, `REVIEW_VERSION =
'novelty_assessment_review_v8_isolated_targets'`.

## Logic

### Step 1 — the coverage gate: is there enough evidence to judge at all?

For each target the code lays out its shortlist and marks every paper
`reviewed` (stage 9 published a pair for this target and the relationship is
not `insufficient_evidence`), `withheld` (audited but not published),
`skipped`, `failed`, or `invalidated`. If fewer than `threshold` of the
papers are `reviewed`, the target's coverage is *incomplete* and the
non-reviewed papers are listed as blockers. The point is to prevent a
confident "low prior overlap" verdict that is really "we only managed to
read a third of the shortlist". An incomplete target can still be judged,
but its judgment is capped: it cannot be `LOW_PRIOR_OVERLAP`, and the critic
will later refuse to approve it. (`build_packet`,
`novelty/assessment_evidence.py:13`; `validate_draft`, `:121`.)

### Step 2 — judge each hypothesis in isolation

With `individual_assessments` on (the default, contract `isolated_v1`), each
hypothesis gets its own model call with a packet containing *only* its
evidence rows and the passages they cite (plus other passages on the same
pages for context). The model must pick one finding and justify it from the
claims. The finding vocabulary is deliberately graded:

- `ALREADY_STUDIED` — a prior paper directly and empirically does this; the
  code requires a *direct, same-target* relationship from stage 9 to exist
  before accepting this label.
- `PARTIAL_OVERLAP` — the prior work covers the regime or mechanism but not
  the hypothesis's specific claim; the model must state the
  `meaningful_difference`.
- `COMPONENTS_KNOWN` — the pieces exist separately; the combination does not.
- `LOW_PRIOR_OVERLAP` — nothing close; only allowed with complete coverage.
- `UNRESOLVED` — the evidence does not permit a judgment.

An independent review call checks the judgment against the same claims;
`revise` gives one more round. The accepted judgment is wrapped by code into
a single-target draft whose action is derived mechanically from the finding
(`ALREADY_STUDIED` → reject, `UNRESOLVED` → defer, otherwise retain). The
reason for isolation is that a model judging four hypotheses at once tends
to let a strong overlap on one colour its reading of the others.
(`hypothesis_packet`, `scope_sources`, `assessment_evidence.py:235-261`;
`_candidate`, `assessment.py:60`; `hypothesis_draft`, `:280`; prompts
`HYPOTHESIS`, `HYPOTHESIS_REVIEW`.)

### Step 3 — lock the hypotheses, then synthesise the direction

The accepted hypothesis judgments become `locked_hypotheses`: the direction
synthesis receives them as fixed inputs and *cannot change them*. Any
hypothesis whose own judgment failed review is locked as `UNRESOLVED`. The
synthesis call then sees the direction, the locked judgments and the cited
passages (not full pages — `synthesis_packet` trims them, which is what
stopped this call from truncating), and must decide the direction's plan:

- `retain` — keep all hypotheses, no text changes;
- `narrow` — keep a subset; may not keep an `ALREADY_STUDIED` one;
- `reframe` — the direction's science must change; requires
  `scientific_edits` (JSON-pointer edits to the proposal);
- `reject` — nothing survives;
- `defer` — cannot be resolved now (e.g. a hypothesis is `UNRESOLVED` for
  lack of coverage) — keep everything pending, change nothing.

The prompt's lifecycle rules (`LIFECYCLE`) say explicitly that retaining a
pending hypothesis never requires inventing an edit to justify it; edits are
for when the science must change. This matters because any edit that touches
a hypothesis sends that hypothesis back for a fresh novelty check, and an
edit made only to "explain" a pending subset used to block every hypothesis
in the direction. (`locked_targets`, `assessment_evidence.py:289`;
`synthesis_packet`, `:262`; prompts `SYNTHESIS`, `LIFECYCLE`.)

### Step 4 — review the synthesis

The same draft/review loop applies: the reviewer sees the locked judgments,
the plan and the evidence and checks that the plan follows (e.g. a `retain`
with an `ALREADY_STUDIED` hypothesis fails; a `reframe` without edits fails).
`reassessment_requests` from the reviewer withhold the synthesis with a
diagnostic. Passage ids are aliased to `srcN` on the wire and restored, as in
stage 9. (`_validated_call`, `assessment.py:35`; `validate_report`,
`assessment_evidence.py:219`.)

### Step 5 — compute the handoff

From the plan the code derives what must happen before a critic can look at
the direction: `requires_novelty_recheck_target_ids` (hypotheses whose text
an edit touched — a direction-level edit under `isolated_v1` does *not*
drag in untouched hypotheses), `unresolved_target_ids`, and `next_stage`:
`research_critic` when nothing is outstanding, otherwise
`direction_revision_then_signature_search_comparison`,
`narrow_direction_and_recheck_scope`, `resolve_coverage`, or `stop`. When
the plan is `retain`/`narrow` with no edits, a `selection_scope` records
which hypotheses the critic should consider in scope.
(`refinement_handoff`, `assessment_schemas.py:217`; `assessment.py:191`.)

## Outputs

`NoveltyAssessmentResult` — `novelty/assessment_schemas.py:247` (`schema_version='novelty_assessment_v1'`).

| Field | Contract |
|---|---|
| `inputs`, `inputs_sha256` | Full `AssessmentInputs` and digest. |
| `candidates: list[CandidateAssessment]` | Per direction: `direction_id`, `candidate_sha256`, `coverage: CoverageRow[]` (per target, `retrieval_status`, per-paper `status` + `reasons`, `complete`, `blockers`), `assessment: AssessmentDraft \| None` (`targets: TargetAssessment[]`, `refinement`), `reviews`, `format_repairs`, `diagnostic`, `hypothesis_assessments` (the per-hypothesis `CandidateAssessment`s). |
| `calls`, `usage`, `run` | Budget, usage, prompt versions, settings, `source_transport='page_grouped_source_map_v2'`. |
| `literature_wide_novelty='unverified'`, `scientific_quality='not_assessed'` | Explicit limits. |

`TargetAssessment`: `target_id`, `finding`, `reasoning`, `evidence` (accepted
claim/passage refs), `meaningful_difference`. `Refinement`: `action`,
`retained_hypothesis_ids`, `removed_hypothesis_ids`, `scientific_edits`.

Persisted as `out/assessment.json`.

## What later stages rely on

- The direction critic runs only when `refinement_handoff.next_stage == 'research_critic'` with nothing unresolved or pending recheck (`critic/evidence.upstream_route`).
- A hypothesis review runs only when its coverage is complete and its status not unresolved, it is not in `requires_novelty_recheck_target_ids`, and it is not in `removed_hypothesis_ids` (`critic/hypotheses.eligibility`).
- `hypothesis_reviews.assessment_sha256 = digest(assessment)` ties stage 16 to this exact result.

## Implementation vs design

§13/§14 are implemented, with the `isolated_v1` contract added: hypotheses
are judged first in isolation and locked before the direction is
synthesised, so a direction-level revision cannot silently change a
hypothesis verdict. The coverage threshold is a code-level gate not present
in the design.
