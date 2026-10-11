# Stage 13 — `portfolio`

Design reference: architecture §16 Final Candidate Output and §17 Final System Output. Implementation notes: `docs/final_portfolio.md`.

## Aim

Render the directions the critic cleared for ranking into a ranked, bounded
portfolio and attach bibliography metadata. Entirely deterministic: no model
calls, no new judgments; pending directions are never promoted to fill the
quota.

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

## Logic (`project`, `portfolio/pipeline.py:62`)

1. For each direction read `source.handoff()['candidates'][direction_id]['next_stage']`:
   `ranking` → eligible (`render_candidate`, `:19`, disposition `reserve`); `stop` → `discarded`; anything else → `pending` (with the critic's diagnostic).
2. Rank eligible directions by `ranking_signals` (`:49`): `KEEP` before `DOWNRANK`, then lower fraction of `concern` findings, lower fraction of `uncertain` findings, then novelty priority (`NOVELTY_PRIORITY` over the assessment finding), then `candidate_id`.
3. Select `eligible[:max_directions]` → disposition `selected`. `selection_shortfall = max(0, min_directions − selected)`.
4. Status: `ready` (selected, nothing pending, no shortfall), `partial` (selected but pending or shortfall), `blocked` (nothing selected, something pending), `empty`.
5. `pending_merges`: MERGE plans proposed by the critic's portfolio review, reported but not applied.
6. `build_portfolio` adds `MetadataSnapshot`s and `PaperReference`s from the catalogues and wraps everything with `source_sha256 = digest(source)`.

## Outputs

`FinalPortfolio` — `portfolio/schemas.py:88` (`schema_version='final_portfolio_v2'`).

| Field | Contract |
|---|---|
| `source`, `source_sha256` | Full reviewed lineage and digest; the `exact_projection` validator recomputes `project(...)` on load. |
| `settings` | Selection policy used. |
| `candidates: list[FinalCandidate]` | **Selected** directions only, rendered with proposal, critique summary and novelty outcome. |
| `ranking: list[RankingEntry]` | All eligible directions in rank order with their signals. |
| `dispositions: list[CandidateDisposition]` | Every direction: `selected | reserve | discarded | pending` + reason. |
| `pending_merges: list[MergePlan]` | Unapplied merge proposals. |
| `status`, `selection_shortfall`, `diagnostics`, `counts` | `counts` = selected / reserve / discarded / pending / generated / eligible. |
| `references`, `metadata_inputs`, `metadata_inputs_sha256` | Bibliography. |
| `ranking_policy='reviewed_signals_v1'`, `unassessed_ranking_dimensions`, `literature_wide_novelty='unverified'` | Explicit limits (importance, technical depth, feasibility, impact are not scored). |

Persisted by `run_pipeline` as `out/portfolio.json` (checkpoint),
`out/final_portfolio.json` (bare result) and `out/final_portfolio.md`
(`portfolio/render.markdown`, with the hypothesis-review table appended when
`hypothesis_reviews.json` exists). `summary.json` gets `status`, `counts`,
`selection_shortfall`, `diagnostics` from here before stage 16 overrides
`status`.

## What later stages rely on

- Stage 16 does not read the portfolio; it reads `critic.json` and `hypothesis_reviews.json`. The portfolio is the direction-level deliverable, stage 16 the hypothesis-level one.

## Implementation vs design

§16/§17 are implemented as a projection. Deviations: the ranking uses only
reviewed signals (critic actions/findings, novelty finding); the design's
richer dimensions are listed under `unassessed_ranking_dimensions` instead of
being scored. Merges are proposals, never executed.
