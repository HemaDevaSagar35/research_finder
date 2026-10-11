# Stage 11 — `critic` (direction critique + independent hypothesis reviews)

Design reference: architecture §15 Research Critic. Implementation notes: `docs/research_critic.md`.

## Aim

Novelty asks "has this been done?"; the critic asks "is this good science?"
For each direction, and separately for each hypothesis whose novelty
evidence is complete, a model reviews the proposal against eight fixed
criteria — is the gap real, is the distinction from prior work meaningful,
is the mechanism plausible, is it testable, is it falsifiable, would a
negative result teach something, and so on — and returns one of four
actions: `KEEP`, `DOWNRANK`, `REFINE` (with the exact changes required) or
`DISCARD`. A second, independent model audits the critique itself: is the
praise earned, is the criticism grounded, did the critic miss a defect in
the proposal or in the upstream evidence? Hypotheses are reviewed on their
own so that a sound hypothesis can be approved even when its direction as a
whole is not.

## Inputs

Two entry points, both run in the `'critic'` branch of `StageRunner.execute` (`research/pipeline.py:183`):

| Entry point | Signature | Where |
|---|---|---|
| Direction critic | `ResearchCritic(store, chat=None, *, review_chat, provider, model, review_model, settings).run(novelty, *, recovery=None) -> CriticResult` | `critic/pipeline.py:65`, `run` at `:164` |
| Hypothesis reviews | `review_hypotheses(novelty, store, *, existing=None, recovery=None, critic_factory=ResearchCritic, **kwargs) -> dict` | `critic/hypotheses.py:26` |

| Input | Contract |
|---|---|
| `novelty: NoveltyAssessmentResult` | Stage 10. |
| `settings` | `PipelineConfig.critic` = `novelty.comparison.Settings` (`concurrency`, `max_calls`, `max_input_tokens`, `max_output_tokens`). |
| `recovery` | Prior `CriticResult` / prior `hypothesis_reviews` dict; a saved critique is reused when its packet is unchanged. |
| `existing` | The fresh `CriticResult`, so a hypothesis can inherit its direction's published action instead of a new call. |

Contracts (`critic/schemas.py`):

| Model | Contract |
|---|---|
| `Action` (`:16`) | `KEEP` (pursue unchanged), `REFINE` (needs listed revisions), `DOWNRANK` (sound, lower value), `DISCARD` (substantive defect). |
| `Finding` (`:19`) | One of 8 `CRITERIA` (`gap_support`, `meaningful_distinction`, `combination_justification`, `mechanism_plausibility`, `testability`, `falsifiability`, `negative_result_value`, `research_contribution`); `assessment` `adequate|concern|uncertain|not_applicable`; `basis` `source_fact|scientific_inference|proposal_analysis`; `reasoning`; `passage_ids`; `proposal_paths`. |
| `TargetCritique` (`:28`) | `target_id`, `action`, `rationale`, `findings` (exactly 8), `learning_if_negative`, `recommended_next_step`. |
| `RevisionRequest` (`:37`) | `target_id`, `field_path` (pointer into the proposal), `required_change`, `reason`. Required iff action is `REFINE`. |
| `UpstreamRequest` (`:44`) | `stage` `direction_correctness|novelty_comparison|novelty_assessment`, `target_id`, `reason`, `basis` `source_grounding|proposal_consistency`, `passage_ids` / `field_paths`. |
| `CritiqueDraft` (`:61`) | `targets`, `revisions`, `upstream_requests`. |
| `ReviewReport` (`:80`) | `decision` `pass|revise|abstain`, `summary`, `issues`, `target_checks`, `test_link_checks`, `upstream_requests`. |
| `CandidateCritique` (`:132`) | `direction_id`, `candidate_sha256`, `packet`, `packet_sha256`, `critique`, `reviews: ReviewRecord[]`, `format_repairs`, `diagnostic`, `blocked_at` `upstream|source_loading|critique|None`. |

Prompts (`critic/prompts.py`): `CRITIQUE`, `REVIEW`, `PORTFOLIO`,
`PORTFOLIO_REVIEW`; `VERSION='research_critic_v3'`,
`REVIEW_VERSION='research_critic_review_v2'`.

## Logic

### What the critic is given, and what it is not allowed to do

The critique packet contains the proposal, the opportunity it came from, the
novelty assessment (findings and the "meaningful difference" for each
target), the audited claims and passages from stage 9, and the source pages
of the direction's own papers. Every finding the critic writes must say what
it rests on: a `source_fact` must cite passages, a `proposal_analysis` must
point into the proposal by JSON pointer, and a `scientific_inference` is
labelled as the critic's own reasoning. A `REFINE` must be accompanied by
concrete `RevisionRequest`s — field, required change, reason — and *only* a
`REFINE` may carry them; a `DISCARD` must rest on at least one `concern`
finding. The critic cannot approve novelty (that was stage 10) and cannot
correct the evidence; if it believes the evidence or the proposal text is
defective, it must file an `UpstreamRequest` naming the stage that owns the
defect. (`packet_for`, `critic/evidence.py:21`; `validate_draft`, `:112`.)

### Direction critic

**Gate.** A direction is critiqued only if stage 10 handed it over cleanly:
`next_stage == 'research_critic'`, nothing unresolved, nothing awaiting a
novelty recheck. Otherwise it is recorded as `blocked_at='upstream'` with the
handoff's diagnostic and no model call is made — critiquing a direction whose
novelty is still open would produce a verdict that has to be thrown away.
(`upstream_route`, `evidence.py:11`.)

**Draft.** The critic model produces a `TargetCritique` for the direction
and for every hypothesis (eight findings each), plus revisions and upstream
requests. (`_reviewed`, `critic/pipeline.py:99`; prompt `CRITIQUE`.)

**Independent review.** A second model, with the same packet and the
critique, is asked to audit the *critique*: unsupported praise (a finding
marked `adequate` the evidence does not support), unsupported criticism,
internal contradictions, and — importantly — defects in the proposal or
upstream evidence that the critic should have caught. It returns `pass`,
`revise` (with issues pointing into the critique by path) or `abstain`, and
may file its own `upstream_requests`. (prompt `REVIEW`; `validate_review`,
`evidence.py:182`.)

**Exit rule.** This is the rule that decides most outcomes:

- If the draft **or** the reviewer filed any `upstream_requests`, the
  critique is withheld with `Source defect requires upstream reassessment.`
  — regardless of the action. The logic is that a `KEEP` on a proposal with
  a known inconsistency between a hypothesis and its experiment is not a
  `KEEP` that should be published; the inconsistency has to be fixed first,
  by the stage that owns the text. In the normal pipeline nothing fixes it,
  so the hypothesis stays pending.
- If the reviewer `pass`es, the draft is accepted.
- If the reviewer says `revise`, the critic gets the issues and one more
  round; the revised draft may change only the targets the reviewer flagged
  (`preserve_unaffected`), so a reviewer's objection to one hypothesis cannot
  be used as cover to rewrite the rest.
- `abstain`, or a second non-pass, withholds the critique with the
  reviewer's summary as the diagnostic.

(`pipeline.py:130-139`; `preserve_unaffected`, `:46`.)

**Format repairs.** Any model response that fails schema or reference
validation is sent back once with the invalid output and the errors; a
second failure fails that call. (`_call`, `pipeline.py:75`.)

**Routing.** From an accepted critique the code derives the direction's next
step, by *consequence* rather than label: direction `DISCARD` → `stop`; any
revision anywhere (an experiment revision counts for every hypothesis linked
to it) → `direction_revision_then_correctness_and_novelty`; hypothesis
`DISCARD`s without revisions → `narrow_direction_and_recheck_scope`;
otherwise → `ranking`. So a direction marked `KEEP` with one hypothesis
marked `REFINE` is not yet rankable. Withheld critiques route to
`resolve_upstream` or `retry_critic`. (`CandidateCritique.handoff`,
`critic/schemas.py:143`.)

**Portfolio pass.** Directions that reached `ranking` are shown together to
the critic model, which may propose merges (two directions that are really
one) with a rationale; a reviewer audits the proposal. Merges are proposals
only; nothing is merged automatically. With fewer than two rankable
directions the step is skipped. (`portfolio_packet`, `evidence.py:202`;
prompts `PORTFOLIO`, `PORTFOLIO_REVIEW`.)

### Hypothesis reviews

**Why they exist.** Under the rules above a single weak hypothesis, or a
single upstream request, blocks the direction's critique. The hypothesis
reviews let each hypothesis be judged on its own evidence so one sound
hypothesis can be published while its siblings are pending.

**Eligibility.** A hypothesis is reviewed only if its novelty judgment is
usable: there is an accepted assessment (its own or the direction's); its
coverage is complete and its novelty status is not `unresolved` (otherwise
`Incomplete novelty evidence: <blockers>` — the message names coverage even
when the cause is an unresolved judgment); it is not awaiting a novelty
recheck because an edit touched it (`Proposed scientific changes require a
fresh novelty check.`); and the assessment did not ask for its removal.
(`eligibility`, `critic/hypotheses.py:8`.)

**Inherit or review.** If the direction's critique *was* published, the
hypothesis simply inherits that critique's action for its target — there is
no reason to ask again. Otherwise the hypothesis gets its own packet, scoped
to its evidence and passages only (`scope_sources`), with
`review_target_ids=[hid]` and an instruction that the rest of the direction
is context: other hypotheses' unresolved status is not a veto, but a shared
source defect or an experiment dependency still is. The same draft → review
→ exit-rule loop runs on that packet. (`hypotheses.py:40-66`;
`packet_for(..., hypothesis_id=)`, `evidence.py:21`.)

**Status.** `KEEP` / `DOWNRANK` → `approved`; `DISCARD` → `discarded`;
anything else → `pending`, with the diagnostic saying why: `Critic requests
REFINE`, `Source defect requires upstream reassessment.`, or the error text
of a failed call.

## Outputs

`CriticResult` — `critic/schemas.py:181` (`schema_version='research_critic_v2'`).

| Field | Contract |
|---|---|
| `novelty`, `novelty_sha256` | The input assessment and its digest. |
| `candidates: list[CandidateCritique]` | Per direction. |
| `portfolio: PortfolioCritique` | `packet`, `assessment: PortfolioDraft` (`considered_direction_ids`, `merges`, `rationale`), `reviews`, `diagnostic`. |
| `calls`, `usage`, `run` | Budget, usage, models, prompt versions. |

Persisted as `out/critic.json`.

`hypothesis_reviews.json` (not a `STAGES` checkpoint; written by `run_pipeline`
at `pipeline.py:280`):

```json
{"schema_version": "independent_hypothesis_reviews_v1",
 "assessment_sha256": "<digest(assessment)>",
 "hypotheses": [{"direction_id", "hypothesis_id", "proposal", "status", "diagnostic",
                 "critique", "reviews", "packet"}]}
```

## What later stages rely on

- `portfolio` ranks directions by `handoff()['next_stage']` and the critique's actions/findings.
- Stage 16 publishes a hypothesis as `approved` only when its last review `pass`ed, the action is `KEEP`/`DOWNRANK`, and no `upstream_requests` exist anywhere in its history.
- `validate_result` (`evidence.py:273`) re-checks all digests when a saved `critic.json` is loaded.

## Implementation vs design

§15 is implemented with three additions: the independent review call on every
critique, the hard "upstream request → withhold" rule (a reviewer's
`proposal_consistency` request voids even a `KEEP`), and per-hypothesis
reviews. Because `refinement` does not run in `run_pipeline`, `REFINE` is a
terminal state in the normal pipeline: the hypothesis stays `pending` with the
proposed revisions recorded.
