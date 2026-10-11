# Stage 12 — `refinement` (not executed by `run_pipeline`)

Design reference: architecture §15 Research Critic (the "revise and re-check" loop) and §16 Final Candidate Output.

## Aim

When the critic says `REFINE`, something has to apply the requested changes
and check them, or the verdict just sits there. This loop is that something:
it rewrites the direction according to the critic's revision requests (and
any correctness requests the reviewer filed), has the rewrite reviewed for
correctness like a fresh draft, works out whether the change invalidates the
novelty evidence, re-runs novelty where it does, and then runs a fresh critic
on the result. It is bounded to one or two cycles so a proposal cannot be
polished indefinitely into approval.

**Status in the pipeline:** `run_pipeline` builds its stage list as
`[s for s in STAGES if s != 'refinement']` (`research/pipeline.py:247`), so
this stage never runs in a normal query run and `refinement.json` is never
written. `PipelineConfig.max_refinement_cycles` defaults to 0
(`pipeline.py:64`) while `RefinementLoop` requires `max_cycles ∈ {1, 2}`
(`critic/refinement_loop.py:163`). The loop is reachable only through
`StageRunner.execute('refinement', state)`, `tools/validate_refinement_loop.py`,
and tests. Everything below describes what it does when it is invoked.

## Inputs

Entry point: `RefinementLoop(store, chat=None, *, review_chat, provider, model, review_model, settings, retriever, max_cycles=2, checkpoint=None, search_settings).run(original, *, observations=None) -> RefinementResult`
— class `critic/refinement_loop.py:160`, `run` at `:198`. Stage wiring (if
invoked): `RefinementLoop(self.store, settings=c.refinement,
retriever=self.get_retriever(), max_cycles=c.max_refinement_cycles,
search_settings=c.novelty_search, **common).run(state['critic'])`
(`pipeline.py:189`), followed by a refresh of `hypothesis_reviews` against
`current_critic(refined)`.

| Input | Contract |
|---|---|
| `original: CriticResult` | Stage 11 output. |
| `observations: dict[str, list[UpstreamRequest]]` | Manual correctness concerns per direction; default `{}`. |
| `retriever`, `search_settings` | Needed when a revision invalidates evidence and a fresh novelty search is required. |
| `settings` | `PipelineConfig.refinement` (same `Settings` as the critic). |

## Logic

### What counts as an authorised change

The loop will only edit a proposal on the strength of a *published*
critique's revision requests, a `direction_correctness` upstream request, or
a manual observation passed in by the operator. Requests from a critique
that was itself withheld, or upstream requests aimed at novelty stages, are
not authorisations to edit — they are blockers that need a different fix.
This keeps the loop from acting on a critic opinion that did not survive
review. (`requests_for`, `refinement_loop.py:127`; `validate_observations`, `:115`.)

### Phase 0 — evidence repair (isolated assessments only)

If the assessment was produced under `isolated_v1`, the critic may have
filed `novelty_comparison` concerns ("this claim was read as X; the passage
says Y"). Before touching any proposal text, up to `max_cycles` repair
rounds reinterpret those comparisons, re-run the assessment on the repaired
evidence, and re-run the critic. If that alone resolves the critique, no
rewrite is needed. (`critic/repair_routes.py:58`.)

### Each cycle

1. **Sort the directions.** For every direction the current critic's
   handoff says what it needs: a *subset* (some hypotheses discarded → narrow
   the direction to the survivors), a *revision* (revision requests or
   correctness requests → rewrite), or a *blocker* (anything else → cannot
   proceed in this loop).
2. **Rewrite.** A model applies the requested edits to the proposal —
   exactly those, nothing else — and the rewrite is reviewed for correctness
   with the same judge and test-link checks as stage 7. (`directions/revision.py`,
   `directions/subset.py`.)
3. **Decide what novelty evidence survives.** For each changed hypothesis the
   code asks whether the stage-9 comparisons still apply: if an edit only
   touched wording outside what the signature captured, the evidence is
   reusable and is re-bound to the new proposal with a certificate saying
   why; if the edit changed the claim, the hypothesis goes back through
   signature → search → comparison. (`assess_reuse`; `_refresh`.)
4. **Reassess and re-critique.** `NoveltyAssessor.run` on the (partly
   refreshed) evidence, then `ResearchCritic.run` with the previous critic as
   recovery so untouched directions are not re-critiqued.
5. Record the cycle (`input_critic_sha256`, revisions, certificates,
   generation, critic, fresh search/comparisons, diagnostic).

### Stopping

The loop stops when every direction's route is `ranking` or `stop` (status
`ready`), when the cycle budget is spent with routes still open
(`needs_revision`), or when a cycle fails or a revision cannot be applied
(`blocked`). (`ready`, `refinement_loop.py:110`.)

## Outputs

`RefinementResult` — `critic/refinement_loop.py:37` (`schema_version='refinement_loop_v1'`).

| Field | Contract |
|---|---|
| `original`, `original_sha256` | The input critic and its digest. |
| `observations` | Manual upstream observations used. |
| `repair_rounds: RepairRound[] \| None` | ≤2 evidence repair rounds. |
| `cycles: Cycle[]` | ≤2 revision cycles. |
| `status` | `ready | needs_revision | blocked`. |
| `diagnostic`, `calls`, `usage`, `run` | |

If it were run through the stage runner it would be checkpointed as
`out/refinement.json` and `portfolio` would consume it via
`state.get('refinement', state['critic'])`; `portfolio.pipeline.current_critic`
picks the latest cycle's critic.

## Implementation vs design

The design assumes the critic's revisions are applied automatically before
final output. The code implements that loop but the query pipeline disables
it (see `README.md`: "The automatic post-critic refinement loop is disabled").
As a consequence every `REFINE` verdict in a normal run is published as
`pending` with `automatic_revision=false` in stage 16.
