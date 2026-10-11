# Stage 13 — `portfolio`

Design reference: architecture §16 Final Candidate Output and §17 Final System Output. Implementation notes: `docs/final_portfolio.md`.

## Aim

Turn the critic's per-direction verdicts into the deliverable at the
direction level: a ranked shortlist of the directions that cleared review,
bounded in size, with a bibliography. The stage is a projection of saved
decisions. It makes no new judgment, calls no model, and — importantly — does
not fill an under-sized portfolio with directions that are still pending.

## Inputs

Entry point: `build_portfolio(source, settings=None, *, metadata_paths=(), store=None) -> FinalPortfolio`
— `portfolio/pipeline.py:114`. Pipeline (`research/pipeline.py:198`):
`asyncio.to_thread(build_portfolio, state.get('refinement', state['critic']), c.selection, metadata_paths=paths, store=self.store)`.

| Input | Contract |
|---|---|
| `source: CriticResult \| RefinementResult` | In normal runs always the critic (refinement does not run). `current_critic(source)` (`pipeline.py:13`) resolves the latest critic inside a refinement result. |
| `settings: SelectionSettings` (`portfolio/schemas.py:16`) | `min_directions` 3 (≥0), `max_directions` 5 (≥1); `min ≤ max`. From `PipelineConfig.selection`. |
| `metadata_paths` | Paper metadata catalogues; for the local backend the pipeline appends `<index_dir>/papers.jsonl` and `<index_dir>/../papers/metadata.json` when they exist. |
| `store` | Optional `PaperStore` for hash-matched local metadata. |

## Logic

### Step 1 — classify every direction by where the critic left it

The critic's handoff gives each direction a `next_stage`. The portfolio
reads that and nothing else to decide eligibility: `ranking` means the
critique was accepted with no outstanding revisions or removals, so the
direction is eligible; `stop` means the critic discarded it; anything else
(`resolve_upstream`, `retry_critic`, `direction_revision_...`, `narrow_...`)
means work is still outstanding, so the direction is `pending` and carries
the critic's diagnostic as its reason. Eligible directions are rendered into
a `FinalCandidate` — proposal, critique summary, novelty finding — and start
with disposition `reserve`. (`project`, `portfolio/pipeline.py:62`;
`render_candidate`, `:19`.)

### Step 2 — rank the eligible ones on reviewed signals only

The ranking uses only things a reviewer actually judged, in this order: the
critic's action (`KEEP` before `DOWNRANK` — a downranked direction is
publishable but the critic said it is less valuable); then the fraction of
the eight criteria marked `concern`; then the fraction marked `uncertain`;
then the novelty finding (`LOW_PRIOR_OVERLAP` ahead of `COMPONENTS_KNOWN`
ahead of `PARTIAL_OVERLAP`); then the direction id as a deterministic
tie-break. Dimensions the pipeline never assessed — importance, technical
depth, feasibility, potential impact — are deliberately *not* invented here;
they are listed in the output as unassessed. (`ranking_signals`,
`pipeline.py:49`; `NOVELTY_PRIORITY`, `:8`.)

### Step 3 — select, and refuse to pad

The top `max_directions` become `selected`; the rest of the eligible list
stays `reserve`. If fewer than `min_directions` were eligible, the shortfall
is reported as a number and a diagnostic — pending directions are never
promoted to make the count. The portfolio's `status` summarises this:
`ready` (selected, nothing pending, no shortfall), `partial` (selected but
something pending or a shortfall), `blocked` (nothing selectable but things
pending), `empty`.

### Step 4 — attach merges and bibliography

The critic's portfolio pass may have proposed merging directions; those
plans are attached as `pending_merges`, reported but not applied — a merge
would create a new proposal that nobody has reviewed. Paper metadata for
every cited paper is pulled from the catalogues into `references`, with a
hash of the catalogue inputs so the bibliography is reproducible.
(`build_portfolio`, `pipeline.py:114`.)

### Integrity

The result stores `source_sha256 = digest(source)` and a validator
recomputes the projection from `source` on load; a `final_portfolio.json`
whose ranking does not match its own critic is rejected. (`exact_projection`,
`portfolio/schemas.py`.)

## Outputs

`FinalPortfolio` — `portfolio/schemas.py:88` (`schema_version='final_portfolio_v2'`).

| Field | Contract |
|---|---|
| `source`, `source_sha256` | Full reviewed lineage and digest. |
| `settings` | Selection policy used. |
| `candidates: list[FinalCandidate]` | **Selected** directions only. |
| `ranking: list[RankingEntry]` | All eligible directions in rank order with their signals. |
| `dispositions: list[CandidateDisposition]` | Every direction: `selected | reserve | discarded | pending` + reason. |
| `pending_merges: list[MergePlan]` | Unapplied merge proposals. |
| `status`, `selection_shortfall`, `diagnostics`, `counts` | `counts` = selected / reserve / discarded / pending / generated / eligible. |
| `references`, `metadata_inputs`, `metadata_inputs_sha256` | Bibliography. |
| `ranking_policy='reviewed_signals_v1'`, `unassessed_ranking_dimensions`, `literature_wide_novelty='unverified'` | Explicit limits. |

Persisted by `run_pipeline` as `out/portfolio.json` (checkpoint),
`out/final_portfolio.json` (bare result) and `out/final_portfolio.md`
(`portfolio/render.markdown`, with the hypothesis-review table appended when
`hypothesis_reviews.json` exists). `summary.json` gets `status`, `counts`,
`selection_shortfall`, `diagnostics` from here before stage 16 overrides
`status`.

## What later stages rely on

- Stage 16 does not read the portfolio; it reads `critic.json` and `hypothesis_reviews.json`. The portfolio is the direction-level deliverable, stage 16 the hypothesis-level one. A run can have an empty portfolio and approved hypotheses, or the reverse.

## Implementation vs design

§16/§17 are implemented as a projection. Deviations: the ranking uses only
reviewed signals; the design's richer dimensions are listed under
`unassessed_ranking_dimensions` instead of being scored. Merges are
proposals, never executed.
