# Final candidate reports and research portfolio

Architecture sections **16–17**, and component contracts 20–21, are implemented
in `portfolio/`. `build_portfolio()` accepts a saved `CriticResult` or
`RefinementResult` and produces a typed, ranked `FinalPortfolio`. This is a
source-preserving projection and selection step: it makes no model calls and
introduces no new scientific claims.

## Usage

```bash
uv run python -m portfolio \
  --critic /path/to/critic.json \
  --out /path/to/portfolio.json \
  --markdown /path/to/portfolio.md

uv run python -m portfolio \
  --refinement /path/to/refinement.json \
  --min-directions 3 --max-directions 5 \
  --out /path/to/portfolio.json
```

Output paths must be new and JSON/Markdown paths must differ. An incomplete or
blocked portfolio is still saved with all actual routes. Exit status is 0 for
`ready`, 1 for `partial`, `blocked`, or `empty`. Invalid input fails validation.
The CLI loads local JSON only; it needs no provider credentials, index, or live
source service.

```python
from portfolio import build_portfolio, SelectionSettings

result = build_portfolio(reviewed_critic_or_refinement,
    SelectionSettings(min_directions=3, max_directions=5))
```

The service can call this function after the critic or bounded refinement loop.
End-to-end API deployment is still separate work.

## Section 16: candidate contract

Each selected candidate preserves its exact reviewed title, direction, mechanism,
scope, assumptions, grounded rationale, opportunity question, evidence records,
paper roles and page hashes. `supporting_evidence` includes the opportunity's
supporting and contextual records; `evidence_paper_roles` distinguishes their
roles. No publication year is invented from an identifier.

Each hypothesis carries the original proposal, its accepted novelty assessment,
coverage, and scientific critique. Every suggested experiment retains its stage,
ID, hypothesis links, comparisons, observations and informative outcomes. Risks,
uncertainties, falsification criteria, the critic's recommended next step and the
generator's recommended experiment ID remain explicit.

`closest_prior_work` preserves **all** accepted target/paper comparisons, sorted
by SAME, VERY_CLOSE, PARTIAL_OVERLAP, ADJACENT, DIFFERENT, then unresolved; ties
use paper and target IDs. It includes dimensions, relationships, source claims
and passage references. A target-wide meaningful difference is retained in the
novelty assessment; it is not misrepresented as a separately reviewed difference
against each individual paper. No new `PROMISING` verdict is invented.

The JSON embeds the full source critic/refinement artifact and its digest. It is
intentionally larger than the Markdown report. Loading a `FinalPortfolio`
revalidates the upstream lineage and recomputes the entire output projection and
selection; edits to reports, ranks, status or dispositions are rejected unless
they match the validated source and recorded settings. This detects inconsistent
artifacts, not adversarial forgery of an entire review history.

## Section 17: selection and ranking

Only a final handoff of `ranking` is eligible. Withheld critique, pending
correctness or novelty work, hypothesis removal, unresolved portfolio review,
and pending merges cannot advance. A merge plan is shown as pending until the
upstream merged proposal passes correctness, affected novelty checks and fresh
critique. DISCARD/stop candidates remain in the disposition ledger. Provider
failures remain pending rather than being relabeled as scientific rejection.

For a refinement result, selection uses the latest completed critic and the
refinement result's own handoff, including its blocked-candidate overrides.

The default is a desired minimum of 3 and a maximum of 5 directions. The ranker
uses the explicit deterministic policy `reviewed_signals_v1`, in this order:

1. KEEP before DOWNRANK. A hypothesis-level DOWNRANK also downranks its direction.
2. Lower fraction of scientific criteria marked `concern` across the direction
   and hypotheses, excluding `not_applicable` criteria.
3. Lower fraction marked `uncertain` using the same denominator.
4. Direction novelty: LOW_PRIOR_OVERLAP, PARTIAL_OVERLAP, COMPONENTS_KNOWN,
   ALREADY_STUDIED, in that order.
5. Candidate ID for stable ties.

These are selection preferences, not calibrated probabilities or scientific
quality scores. The saved ranking contains each signal. Importance, technical
depth, feasibility and potential impact are explicitly unassessed: the upstream
contracts do not provide reviewed scores for them. This first policy uses
available reviewed judgments instead of inventing those assessments.

All eligible candidates appear in the ranking. The first `max_directions` receive
full final reports; the rest are reserves, with their complete source artifacts
available in the embedded input. Every generated candidate has exactly one
selected/reserve/discarded/pending disposition. Pending merge plans and original
upstream routes are preserved.

A nonempty selection is `ready` only when the desired minimum is met and no
candidate is pending; otherwise it is `partial`. A zero selection is `blocked`
when work is pending, or `empty` when no candidate can advance and none is pending.
A shortfall is explicit. The upstream generator supplies the larger internal
candidate pool; this final stage does not generate filler hypotheses, silently
repair proposals, or force the example 8→3–5 counts from the architecture.

## Validation — 2026-10-09

18 local integration tests cover exact proposal/evidence preservation, JSON
roundtrip and tamper detection, KEEP/DOWNRANK/DISCARD/refinement/narrowing routes,
seven-candidate ranking and reserves, unresolved merge plans, withheld portfolio
review, refinement input, CLI output and overwrite protection.

An offline replay of the existing
`/home/hema/research_runs/momentkv_reassessment_v13/critic.json` correctly produces
`status=blocked`, zero selected, one pending, and the original `resolve_upstream`
route. Its unresolved H4/E4 issue is not turned into a published candidate.
No new provider calls or scientific correction cycles were run for this stage.

Final regression: **607 tests plus 13 subtests passed**, with one existing
faiss/NumPy deprecation warning. `git diff --check` and the CLI help check pass.


### Additional section 16–17 checks — 2026-10-09

The focused suite now passes **25 tests**. Seven new cases cover mixed
selected/reserve/discarded/pending outcomes, selection caps of 1/3/10 with stable
ranking, successful and rejected refinement cycles, preservation of the final
revised experiments, Markdown claim/citation/falsification content, unchanged
source inputs, and colliding CLI output paths.

A seven-candidate synthetic scenario returns exactly 3 selected, 2 reserves,
1 discarded and 1 pending. Its status remains `partial` even though the requested
minimum is met, because a scientific revision is outstanding. These cloned
fixture candidates test routing and rendering, not scientific diversity or quality.

Three saved real runs were replayed: `research_critic_kv_live_v6`,
`refinement_kv_live_v2`, and `momentkv_reassessment_v13`. Each validated, exported,
and round-tripped successfully, preserving zero selected, one pending, and
`resolve_upstream`. No accepted real portfolio was available in these three
inputs; accepted output is exercised with synthetic fixtures and fake-model
refinement cycles. No fresh model calls were made.

Artifacts and replay timings are saved under
`/home/hema/research_runs/portfolio_validation_20261009/`, including
`replay_summary.json`, three real-run JSON/Markdown pairs, and
`synthetic_portfolio.json` / `synthetic_portfolio.md`. No production-code defects
were found by these additional checks. The first test invocation failed during
temporary-directory setup because its parent did not exist; creating the parent
and rerunning yielded 25 passing tests. `git diff --check` passes.
