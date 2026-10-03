# Opportunity Miner

Implemented on 2026-10-03 against [architecture section 6](research_path_generator_architecture_refined.md#6-opportunity-miner).
This stage asks what appears unresolved given reviewed cross-paper findings.
It reuses the existing Landscape, CrossPaperReasoning, and PaperStore contracts;
landscape construction and cross-paper reasoning algorithms are unchanged.

## Flow and contract

1. Validate the canonical `landscape_builder_v1` input and the reasoning output's
   exact landscape hash and topic. Accepted findings, observations, and tensions
   are eligible sources; rejected diagnostics and unreviewed landscape statements
   cannot independently support an opportunity.
2. Give the proposer reviewed sources, the landscape items they refine, and
   relevant concept groups. A candidate contains category, unresolved question,
   rationale, scope, uncertainties, and supplied source/evidence IDs. Categories
   are recurring limitation, restrictive assumption, missing regime, contradiction,
   failure mode, missing evaluation, unresolved tradeoff, and mechanistic interaction.
   An empty proposal is valid.
3. Resolve references in code. Evidence IDs are namespaced by their reviewed
   source; each cited source must own at least one cited evidence record. Reject
   unknown, duplicated, or mismatched references. Preserve extracted
   `author_stated`/`model_inferred` origins from the original JSON pointers.
4. Reload selected `paper.json` artifacts. Check their hashes, source values,
   provenance pointers, and page locations against the cited evidence. Nonempty
   exact value prefixes are allowed because the reasoner may truncate bundles.
   Load all selected evidence pages plus all previously reviewed pages for the
   cited reasoning sources, retaining potentially contrary context. Previously
   reviewed page hashes must match. Missing, changed, unresolved, or over-budget
   evidence produces a diagnostic, never partial acceptance.
5. Review the complete candidate against those original pages. The reviewer
   accepts, rejects, or marks it insufficient, checking motivating claims,
   comparability, scope, attribution, and whether reviewed evidence already
   answers the question. Acceptance must cite supplied pages for every supporting
   paper. Code constructs the final provenance from loaded artifacts.

The `opportunities_v1` output contains:

- Exact normalized Landscape and CrossPaperReasoning input hashes.
- Accepted opportunities with candidate text, source IDs and landscape `refines`,
  selected evidence paths/hashes/origins, reviewed page paths/hashes, review notes,
  and explicit `single_paper` or `multiple_papers` support based on selected evidence.
- `verification="supported_in_reviewed_corpus"` and
  `literature_novelty="not_assessed"` on every accepted opportunity.
- Typed diagnostics retaining failed candidates where available, source/batch/
  proposal/acceptance coverage, call attempts, token usage, settings, model names,
  timestamps, and prompt version.

Source attribution records what the extractor classified; it does not prove an
explicit author statement. Page review is still required. An accepted opportunity
is an unresolved question within the reviewed evidence, not a certified novel
research direction. Sections 7 onward generate directions/hypotheses and perform
separate novelty retrieval.

## Usage

```bash
uv sync
uv run python -m opportunities.miner \
  --landscape landscape.json --reasoning reasoning.json \
  --root /srv/research_finder/markdown --out opportunities.json
uv run pytest tests/test_opportunity_miner.py
```

Use the exact Landscape associated with the reasoning output. Hashes include the
normalized schema defaults; if an old artifact's lineage no longer matches after
a schema change, rerun reasoning on the normalized Landscape rather than editing
hashes. The CLI reads local artifacts and writes only the requested output.
The async library accepts an injected `PaperStore` (including its existing S3
factory) and optionally a caller-owned async chat function. Create one miner and
one store per request. An internally created client is closed when the run ends.

Provider/model: `OPPORTUNITY_PROVIDER` / `OPPORTUNITY_MODEL`, otherwise the general
provider and its configured default model. `OPPORTUNITY_REVIEW_MODEL` optionally
selects a separate review model. CLI `--provider`, `--model`, `--review-model`
override these settings.

| CLI flag | Default | Purpose |
|---|---:|---|
| `--concurrency` | 4 | Maximum simultaneous provider calls |
| `--max-calls` | 40 | Atomic total attempt budget including repairs/failures |
| `--batch-size` | 12 | Reviewed sources per proposal batch |
| `--max-batches` | 8 | Maximum proposal batches; omitted sources are diagnosed |
| `--max-candidates-per-batch` | 5 | Maximum proposed candidates per batch |
| `--max-review-pages` | 20 | Maximum complete page set per candidate |
| `--max-input-tokens` | 40000 | Estimated complete prompt limit, including schema |
| `--max-output-tokens` | 12000 | Proposal output allowance; `OPPORTUNITY_MAX_OUTPUT_TOKENS` |
| `--max-review-output-tokens` | 12000 fallback | `OPPORTUNITY_REVIEW_MAX_OUTPUT_TOKENS`, then `REASON_MAX_REVIEW_OUTPUT_TOKENS` |
| `--repair-rounds` | 1 | Additional attempts for malformed/truncated/schema-invalid output |

The current development environment inherits a 128000 review output allowance
from `REASON_MAX_REVIEW_OUTPUT_TOKENS`. Output allowances are separate from input
limits and do not guarantee that a provider supports the requested amount.
Fatal provider rejections stop queued work; already-running calls may complete.
The CLI saves diagnostics and exits with status 2 for a fatal provider error.
Other partial or empty results may exit successfully; consumers must inspect
coverage and diagnostics.

Independent proposal batches run concurrently. Each ready batch starts its
reviews without waiting for other proposals. A shared semaphore and attempt
counter bound calls; blocking artifact access runs through `asyncio.to_thread`,
with per-resource locks to avoid racing the same cached artifact. Required
proposal → reference resolution → page loading → review dependencies remain.

## Validation

`uv run pytest -q`: **126 passed, 13 subtests passed**. The 29 component tests
cover acceptance, lineage mismatch, unknown references, stale artifacts/pages,
fabricated source values under real hashes, missing pages, single-paper support,
required cross-paper citations, rejection/insufficiency, bounded repairs,
truncation, exact duplicates, empty proposals, page/input/call/batch limits,
fatal provider errors, simultaneous reviews, and reviews overlapping another
batch's proposal. The suite reports one existing FAISS/NumPy deprecation warning.

A bounded live run reused the attribution-validation Landscape and its one
accepted cross-paper finding across three papers. It used `deepseek-flash`, a
maximum of eight calls, one batch, three proposed candidates, concurrency four,
and a 128000 review output allowance. Five attempts completed: one proposal,
one repair, and three reviews; no truncation or final diagnostics. Usage was
34152 input tokens and 20190 output tokens (17598 reported reasoning tokens).

| Candidate | Supporting paper | Automated review outcome |
|---|---|---|
| Generalization to larger MoE models and different top-k routing | SemMoE (`432956a1a205`) | Accepted within reviewed model/evaluation scope |
| Compute/communication overlap across multiple nodes | TokenWeave (`b02deed4ad77`) | Accepted with a hardware wording caveat |
| Capacity-routing and multimodal robustness beyond evaluated settings | Capacity-Aware Inference (`9515cc87b003`) | Accepted within reviewed tables/benchmarks |

All three use **single-paper** supporting evidence despite coming from one
cross-paper finding. An independent deterministic audit checked all six selected
evidence records and five unique original review pages against their hashes and
paths, plus both input lineage hashes.

The TokenWeave candidate says “8xH100 and 4xH100 DGX systems”; its reviewer correctly
notes TP-4 uses four GPUs within the same eight-GPU DGX. The candidate was still
accepted as written. This is a concrete limitation of automated scientific
review: retained notes expose the imprecision, but schema and provenance checks
cannot guarantee factual precision in generated prose. Do not interpret three
acceptances as three proven novel or fully human-validated opportunities.

Local artifacts (not checked into Git):

- `/home/hema/research_runs/opportunity_miner_validation/result.json`
- `/home/hema/research_runs/opportunity_miner_validation/provenance_audit.json`
- Inputs: `/home/hema/research_runs/limitation_attribution_validation/landscape.json`
  and `reasoning.json`.

## Current boundaries

Batching follows input source order (findings, tensions, observations); there is
no semantic source clustering or fusion across batches. Deduplication detects
only the same normalized question with the same source set. The live smoke test
covers three missing-evaluation/regime questions, not all eight categories.
No automatic narrowing/redrafting follows substantive review rejection.
Novelty retrieval, ranking, direction/hypothesis generation, and service endpoint
wiring remain separate work. Usage snapshots come from the existing process-wide
tracker; concurrent application requests need request-scoped usage accounting
before those token totals can be treated as isolated per-request measurements.
