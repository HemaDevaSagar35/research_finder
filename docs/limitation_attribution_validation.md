# Limitation attribution: implementation and live validation

## Problem and change

The landscape combined author-stated and extractor-inferred limitations as
plain strings. Paper IDs survived, but attribution and original record paths
did not. A later draft could incorrectly describe an inference as something
an author explicitly acknowledged.

The fix preserves each source's paper ID, extraction origin, exact JSON record
pointer, original statement, and source locations through PaperCard projection
and aggregation. Mixed sources remain separate. The reasoner includes the
source labels in drafting and review; its evidence origin is derived in code
from the actual record path. Author-stated remains an extraction classification,
not independently verified testimony. The review must still inspect the pages.

This change covers limitations. Other evidence kinds, including inferred future
work, retain their existing labels and paths but have `origin: null` in the new
limitation-origin field. Empty legacy source lists are supported; old aggregate
files must be rebuilt to recover exact original source statements and pointers.

## Regression validation

97 tests and 13 subtests passed. New tests cover:

- Original array indices preserved when empty strings are filtered.
- Missing locations remain empty; legacy cards do not invent record paths.
- Origin/path mismatch rejected.
- Mixed author/inferred sources survive aggregation and JSON round-trip.
- Source paper membership validated against supporting papers.
- Attribution reaches the draft, review, and accepted output evidence.
- A reviewer rejection of an explicit-author overclaim remains rejected.

The fake-model regression checks data flow and review enforcement. It cannot
prove that a live model will always preserve attribution correctly.

## Live rerun: 2026-10-03

Reused the same three papers and rebuilt only statement aggregation using the
real provider, with up to four concurrent calls. Extraction, normalization,
retrieval, and the shared index were not rebuilt. The same all-three-paper
limitation cluster was recovered. Its three source records were:

| Paper | Record path | Origin |
|---|---|---|
| Sem-MoE (`432956a1a205`) | `/limitations/inferred/1` | model_inferred |
| Capacity-Aware Inference (`9515cc87b003`) | `/limitations/inferred/0` | model_inferred |
| TokenWeave (`b02deed4ad77`) | `/limitations/inferred/1` | model_inferred |

All source statements and locations matched the original records exactly.
The aggregate was phrased as an apparent evaluation limitation, and retained
these three distinct source references. It was not labeled author-stated.

Reasoning was bounded to one task and four calls, with 12,000 draft output
tokens and the configured 128,000 review output allowance:

- Two calls (draft and review); no truncation.
- All three papers inspected; 12/12 emitted citations resolved.
- Nine requested pages fetched and supplied; none omitted.
- One accepted low-confidence cross-paper finding and one rejected candidate.

The accepted result separated evaluation coverage by paper: model/routing
coverage for Sem-MoE, intra-node tensor-parallel scope for TokenWeave, and
capacity/multimodal coverage for Capacity-Aware Inference. It did not assert
that the authors explicitly admitted these limitations. It retained three
`model_inferred` limitation references alongside other structured evidence.
All six accepted evidence artifact hashes and five accepted review-page hashes
matched the local files.

The other candidate was rejected because its model list was not established
by the particular pages supplied for its citations. This does not establish
that those models are absent from the whole paper: an earlier run's page 8
showed additional models. A rejection is about the candidate's supplied support.

## Interpretation

This rerun shows that attribution survives the pipeline and that the earlier
explicit-author overclaim did not recur in this sample. It does not prove
causality from a single stochastic rerun or establish scientific novelty. The
accepted wording still generalizes from selected evaluation pages; omissions
should remain scoped to reviewed evidence and verified more broadly before
Opportunity Miner promotes them. The low-confidence finding is not a validated
research opportunity.

Artifacts: `/home/hema/research_runs/limitation_attribution_validation/` contains
`aggregation.json`, `landscape.json`, `reasoning.json`, and token usage. The live
harness is `/home/hema/research_runs/run_attribution_validation.py`. No API
credentials are copied into those files.
