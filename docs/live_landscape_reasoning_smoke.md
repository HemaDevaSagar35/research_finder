# Live landscape → reasoning smoke test

Run on 2026-10-03 on `feature/opportunity_miner`, using the shared-contract
integration described in [the design note](landscape_reasoning_integration.md).
No shared artifacts, credentials, or provider defaults were changed.

## Setup

- Topic: `efficient MoE inference`.
- Two planned queries; three retrieved papers; local hybrid index at
  `/srv/research_finder/index`.
- Local `paper.json` and Markdown at `/srv/research_finder/markdown`.
- Chat provider returned model name `deepseek-flash`; corpus query embeddings
  use the index's DeepInfra Qwen configuration. Landscape normalization and
  aggregation use their existing local embedding defaults.
- The run harness calls `build_landscape(topic, contexts)` with PaperCards
  projected from local artifacts. The builder CLI's S3 loader was not tested
  or changed. This validates library composition using real providers.

Papers: TokenWeave (`b02deed4ad77`), Semantic Parallelism / Sem-MoE
(`432956a1a205`), and Capacity-Aware Inference (`9515cc87b003`). All three
artifacts validated and their PaperCards had no missing fields. All 17 distinct
paper/page pairs cited by retrieved records were present locally.

## Landscape

The actual builder output passed directly into the shared schema and reasoner:

| Collection | Count |
|---|---:|
| Groups | 154 |
| Relationships | 55 |
| Aggregated findings | 27 |
| Recurring limitations | 27 |
| Common assumptions | 14 |
| Contradictions | 0 |
| Underexplored regimes | 151 |

Twelve of 55 relationships have resolved record-level evidence. The resolver
only searches retrieved matching records, so absence of a match is not absence
of support in the full paper. Paper support remains explicit; locations were
not fabricated. Sparse-concept counts are relative to these three papers.

## Initial bounded run

Limits: 3 threads, 12 calls, existing 6,000 draft / 3,000 review output tokens.

- 11 calls: 3 draft, 3 review, 5 repairs; 6 truncated responses.
- 49 emitted evidence citations resolved; all 28 requested page instances
  were fetched and supplied (the same page may occur in several threads).
- Zero accepted results; six candidates received `review_invalid` because
  support-review output was truncated, including after repair.
- The scheduler lists relationships first, in builder order. The first three
  were all TokenWeave tasks, so only one paper was actually inspected.
- The remaining 271 of 274 tasks were correctly budget-skipped. A completed
  thread status alone does not imply accepted output or successful review.

This exposed runtime sizing and sampling issues, not a schema handoff failure.

## Focused cross-paper follow-up

Preserved the first run. Selected the one aggregated limitation supported by
all three papers, keeping its original item ID, full paper inventory and groups.
Saved this selection as a separate landscape; output hashes that actual input.
Other collections were empty for this targeted diagnostic run.

Limits: 1 thread, 4 calls, 12,000 draft and review output tokens, 16 review pages.

- Two calls (draft + review), zero truncation, all three papers inspected.
- 24/24 emitted evidence citations resolved.
- 17 requested pages fetched, 16 supplied; one omitted by the page cap.
- One accepted cross-paper finding, with low confidence: the selected papers
  evaluate a limited set of models and benchmarks, including few MoE models.
- One candidate withheld because it required the omitted page.
- One rejected as overbroad: claiming larger-scale MoE models remained untested
  overlooked TokenWeave's Mixtral-8x22B evaluation.

### Manual audit

All 11 accepted evidence references resolved both value and provenance JSON
pointers; their artifact hashes matched. All 11 accepted review-source page
hashes matched. Read evaluation material on Sem-MoE pages 8–9, Capacity-Aware
Inference pages 6/8/10, and TokenWeave pages 9/11.

Those pages establish the named evaluation settings and support a cautious
coverage observation. They do not establish that the scope is inadequate or
that a novel opportunity exists. The phrase "small set" lacks an explicit
comparison baseline. The automated review's counts are also incomplete:
Capacity-Aware Inference page 10 includes an additional Qwen3-MoE evaluation,
beyond the four principal language models listed in its review note. Do not
promote this finding directly into a validated opportunity.

## Usage and limits of this test

Chat accounting across both runs: 26 calls, 145,335 input tokens, 121,809 output
tokens, of which 103,083 were reported as reasoning tokens. This includes query
planning and landscape construction; it excludes embedding API usage. No
currency estimate is asserted. The 12-call bound applied to the initial
reasoner stage, not the builder; the follow-up had its own four-call bound.

The larger review-output allowance worked for this one follow-up. Defaults
remain unchanged; a single run does not justify a global budget increase.
Before Opportunity Miner quality evaluation:

1. Use provider-appropriate reasoning/output budgets, checking truncation.
2. Select representative multi-paper tasks; a prefix cap can select one paper.
3. Prefer specific, scoped observations over generic "limited evaluation" claims.
4. Preserve rejection and omitted-page diagnostics, rather than treating a
   process exit code as a quality pass.

## Artifacts and reproduction

Private local artifacts are under `/home/hema/research_runs/live_integration/`:
`query_plan.json`, `retrieval.json`, `inventory.json`, `landscape.json`,
`reasoning.json`, usage snapshots, and `focused/{landscape,reasoning}.json` plus
`focused/provenance_audit.json`. Run harnesses are in `/home/hema/research_runs/`.
They contain no copied API credentials. These absolute paths are machine-local.

To rerun the focused saved landscape (makes paid provider calls):

```bash
uv run python -m reasoning.cross_paper \
  --landscape /home/hema/research_runs/live_integration/focused/landscape.json \
  --root /srv/research_finder/markdown --no-s3 \
  --max-threads 1 --max-calls 4 \
  --max-draft-output-tokens 12000 --max-review-output-tokens 12000 \
  --out /home/hema/research_runs/focused_rerun.json
```

## Broader asynchronous follow-up

A later run reused the saved landscape and selected eight real items: one
relationship and one aggregated finding per paper, the multi-paper limitation,
and one assumption. No supporting-paper links were fabricated to make an item
cross-paper. The saved subset retained all original groups and paper IDs.

- Four concurrent model calls observed at peak (instrumented around API calls).
- Eight tasks completed; 18 calls: eight drafts, eight reviews, two repairs.
- Review output used `.env`'s `REASON_MAX_REVIEW_OUTPUT_TOKENS=128000`;
  draft output was 12,000 for this test. No response was truncated.
- 13 accepted single-paper observations; no accepted cross-paper findings or
  tensions. Four candidates were rejected on review, and one invalid one-paper
  tension was rejected structurally.
- 126/126 emitted citations resolved; all 72 requested page instances supplied.
- Audit verified 80 accepted evidence references (JSON pointers and artifact
  hashes) and 25 unique review-page hashes. Representative source passages were
  also inspected; this was not exhaustive human review of all claims.
- Chat usage: 159,849 input tokens, 92,897 output tokens (79,283 reasoning).

The genuine multi-paper limitation was rejected because it attributed an
inferred limitation to an explicit statement in the original pages. The
reviewer also rejected overbroad model-generalization and unsupported tuning
claims. This run supports functioning validation and useful scoped observations;
it does not establish reliable cross-paper synthesis or validated opportunities.
For example, an accepted routing observation mentions F1 "up to 1.000", while
the source table includes 0.667–1.000 across layers. Downstream consumers must
retain conditions and source scope rather than treat the maximum as universal.

Artifacts: `/home/hema/research_runs/broader_async_validation/` contains the
selected `landscape.json`, `selection.json`, `reasoning.json`, `concurrency.json`,
and `provenance_audit.json`. The harness closes its async provider client.

## Concurrent implementation

Independent landscape work now overlaps: graph extraction/normalization and
statement aggregation run together, and independent cluster summaries run
concurrently through the async client's semaphore. Synchronous embedding and
similarity work is offloaded from the event loop. Results retain input order;
concurrent calls are drained before owned clients close on failure. A thread's
support review still depends on its draft. This dependency is intentionally
sequential. These builder changes were regression-tested offline rather than
spending calls regenerating the saved live landscape.

Validation after these changes: 90 tests and 13 subtests passed, including
concurrency overlap, deterministic result ordering, and error cleanup.
