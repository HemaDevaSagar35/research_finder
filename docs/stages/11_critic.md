# Stage 11 — `critic` (direction critique + independent hypothesis reviews)

Design reference: architecture §15 Research Critic. Implementation notes: `docs/research_critic.md`.

## Aim

Judge the scientific quality of each direction and of each evidence-ready
hypothesis against eight fixed criteria, grounded in the accepted novelty
evidence and the source pages. Each critique is audited by an independent
review call. The direction critic also proposes cross-direction merges for the
portfolio. Hypotheses are reviewed individually so that one can be approved
without approving its whole direction.

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
| `Finding` (`:19`) | One of 8 `CRITERIA`; `assessment` `adequate|concern|uncertain|not_applicable`; `basis` `source_fact|scientific_inference|proposal_analysis`; `reasoning`; `passage_ids`; `proposal_paths`. |
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

### Direction critic (`ResearchCritic._run`, `critic/pipeline.py:170`)

1. `upstream_route(novelty, direction)` (`critic/evidence.py:11`): read `refinement_handoff`; unless `next_stage == 'research_critic'` with no unresolved / recheck targets → `blocked_at='upstream'`, no call.
2. `packet_for(novelty, direction, support)` (`evidence.py:21`): candidate, novelty assessment and outcomes, opportunity, support pages as passages, optional `selection_scope`. Loading failure → `blocked_at='source_loading'`.
3. `_reviewed(packet)` (`pipeline.py:99`), ≤2 rounds:
   - Draft: task `research_critique` (or `revise_research_critique`) with `CRITIQUE` → `CritiqueDraft`; `validate_draft` (`evidence.py:112`): passage/pointer references resolve, `REFINE` ↔ revisions bijective, `DISCARD` needs a `concern`; revisions must not touch targets the reviewer did not flag (`preserve_unaffected`, `pipeline.py:46`).
   - Review: task `review_research_critique` with `REVIEW` → `ReviewReport`; `validate_review` (`evidence.py:182`).
   - Exit rule (`pipeline.py:130`): if the draft **or** the report carries `upstream_requests` → `diagnostic='Source defect requires upstream reassessment.'`, critique withheld; `pass` → accept; `revise` → one more round; `abstain` → stop.
   - `_call` (`pipeline.py:75`): one format repair (`repair_<task>` with `invalid_output` + `validation_errors`); each recorded as `FormatRepair`.
4. Portfolio: `portfolio_packet` (`evidence.py:202`) over candidates whose `handoff()['next_stage']=='ranking'`; <2 eligible → trivial `PortfolioDraft`, else `_reviewed(packet, portfolio=True)` with `PORTFOLIO` / `PORTFOLIO_REVIEW`.
5. `CandidateCritique.handoff()` (`schemas.py:143`) routes by consequence, not by label: direction action `DISCARD` → `stop`; any `revisions` (on the direction or any hypothesis; an `/experiments/N/...` revision counts for every linked hypothesis) → `direction_revision_then_correctness_and_novelty`; hypothesis `DISCARD`s without revisions → `narrow_direction_and_recheck_scope`; otherwise → `ranking`. So a direction `KEEP` with a single hypothesis `REFINE` is not rankable. Withheld critiques → `resolve_upstream` (blocked upstream or any upstream request in history) or `retry_critic`.

### Hypothesis reviews (`review_hypotheses`, `critic/hypotheses.py:26`)

1. `eligibility(candidate, direction, hid)` (`:8`) returns a blocking diagnostic or `None`: no accepted assessment (direction-level or the hypothesis's own); coverage incomplete **or** the target's novelty status `unresolved` → `Incomplete novelty evidence: <blockers>` (the message names coverage even when the cause is an unresolved judgment); `hid ∈ requires_novelty_recheck_target_ids` → `Proposed scientific changes require a fresh novelty check.`; `hid ∈ removed_hypothesis_ids` → not eligible for independent approval.
2. If the parent direction has a published critique, reuse its target action (no call).
3. Else `packet_for(..., hypothesis_id=hid)` builds a hypothesis-scoped packet (`scope_sources` limits evidence and passages to that target; `review_target_ids=[hid]`) and `critic._reviewed(packet)` runs the same draft/review loop with a scope instruction appended.
4. Status mapping: `KEEP`/`DOWNRANK` → `approved`; `DISCARD` → `discarded`; anything else (`REFINE`, upstream request, failed review) → `pending` with `diagnostic` (`Critic requests REFINE`, `Source defect requires upstream reassessment.`, or the error).

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
