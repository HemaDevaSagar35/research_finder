# Stage 12 — `refinement` (not executed by `run_pipeline`)

Design reference: architecture §15 Research Critic (the "revise and re-check" loop) and §16 Final Candidate Output.

## Aim

A bounded loop that applies the critic's `REFINE` revisions and
`direction_correctness` upstream requests to a direction, re-runs correctness
review, re-validates novelty (reusing comparisons where the evidence is
unaffected, otherwise fresh search + comparison + assessment), and runs a
fresh critic. Its purpose is to turn `REFINE` into `KEEP`/`DOWNRANK`/`DISCARD`
without manual editing.

**Status in the pipeline:** `run_pipeline` builds its stage list as
`[s for s in STAGES if s != 'refinement']` (`research/pipeline.py:247`), so
this stage never runs in a normal query run and `refinement.json` is never
written. `PipelineConfig.max_refinement_cycles` defaults to 0
(`pipeline.py:64`) while `RefinementLoop` requires `max_cycles ∈ {1, 2}`
(`critic/refinement_loop.py:163`). The loop is reachable only through
`StageRunner.execute('refinement', state)`, `tools/validate_refinement_loop.py`,
and tests.

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

## Logic (`RefinementLoop.run`, `refinement_loop.py:198`)

1. Validate `original` and `observations` (`validate_observations`, `:115`).
2. If the assessment contract is `isolated_v1`, run up to `max_cycles` **repair rounds** (`critic/repair_routes.py:58`): reinterpret comparison concerns raised by the critic, reassess novelty, re-run the critic. Recorded as `RepairRound{parent_sha256, evidence_repairs, failures, critic}`.
3. For each cycle while `not ready(current)` (`ready`, `:110`: every handoff route is `ranking` or `stop`):
   - Partition candidates: subsets to narrow (`directions/subset.py`), directions with `direction_correctness` requests or `REFINE` revisions to revise (`directions/revision.py`), and blockers (non-correctness upstream requests, unresolved assessments).
   - `requests_for` (`:127`): only revisions from a *published* critique authorise scientific edits.
   - Revise → fresh correctness review → `assess_reuse` certificates; if all evidence can be reused, rebind; else `_refresh` runs `NoveltySearcher` + `NoveltyComparator` on the changed targets.
   - `NoveltyAssessor.run(...)` then `ResearchCritic.run(assessment, recovery=current)`.
   - Record a `Cycle{input_critic_sha256, revisions, certificates, generation, critic, fresh_search, fresh_comparisons, diagnostic}`.
4. Status: `ready` if `ready(current)`; `blocked` on exception or unresolved revisions; otherwise `needs_revision`.

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
