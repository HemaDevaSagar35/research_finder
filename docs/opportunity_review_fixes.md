# Opportunity review and paper-role fixes

Implemented after [broader validation](opportunity_validation.md) exposed two
failures: reviewers accepting candidates that needed factual corrections, and
context-only papers inflating multiple-paper support.

## Behavior

Review now explicitly returns factual status, required corrections, and one
candidate-relative assessment per selected evidence paper. A correction,
`needs_correction` status, or `revise` decision creates a `requires_correction`
diagnostic. This veto overrides a simultaneous `accept` and is applied before
schema repair when recognizable in the response. The original candidate and
review payload remain in diagnostics. Uncertain factual accuracy also blocks
acceptance. There is no silent rewrite or automatic promotion: a corrected
candidate must receive a fresh original-page review.

The reviewer assigns each selected paper `supporting` or `context_only` relative
to the new opportunity, with rationale, selected evidence IDs, and original
page IDs. Code checks exact paper coverage and citation ownership. At least one
paper must support an accepted candidate. Upstream `supports`/`contradicts`
stances do not determine the new role; contradictory papers can both support a
new question about their discrepancy.

The output is now **`opportunities_v2`**, prompt
`opportunity_v2_review_roles`. `paper_ids` still retains every cited evidence
paper; `supporting_paper_ids`, `context_paper_ids`, and `paper_assessments` are
required new fields. `single_paper`/`multiple_papers` counts only supporting
papers. All selected evidence and previously reviewed contrary pages remain.
Legacy v1 outputs are not silently promoted to v2: re-run mining or explicitly
re-review their candidates. The validation tools read historical candidates
without treating historical acceptance as a new review.

Landscape construction, upstream reasoning, proposal logic, and concurrent
scheduling are unchanged. This adds structure to the existing review call,
not a sequential extra model pass per candidate.

## Offline validation

**149 tests passed, 13 subtests passed**; one existing FAISS/NumPy warning.
Fourteen new regressions verify correction signals independently, an accept
with corrections, a malformed review carrying a veto, uncertain acceptance,
context-only support counts with retained citations, role independence from
upstream stance, missing/duplicate/unknown roles, evidence/page ownership,
legacy review responses, and zero-support acceptance. Existing concurrency,
atomic budget, original-page, and stale-evidence tests continue to pass.

## Focused live validation

32 DeepSeek calls, no truncation, using the same saved real inputs and unchanged
source artifacts. Independent reviews were concurrent. This rechecks the changed
review behavior; it does not regenerate the already-tested landscapes or rerun
unchanged proposal generation.

| Check | Result |
|---|---|
| Eight positive category review controls | 8/8 accepted |
| Six synthetic negative controls | 6/6 withheld with correction diagnostics |
| Real author-attribution and already-answered probes | 2/2 withheld |
| Original inaccurate TokenWeave hardware candidate | 3/3 withheld; previously 3/3 accepted |
| AttentionPack-only question citing STAR-KV for exclusion | 3/3 accepted as single-paper support; STAR-KV retained as context |
| Shared AttentionPack/STAR-KV model-scale question | 3/3 retained as two supporting papers |
| Hardware-only wording correction | 2/3 accepted; 1/3 requested further scope corrections |
| Hardware plus reviewed-page scope correction | 3/3 accepted |

The hardware-only edit preserved an overbroad rationale saying the paper did
not test multi-node scales. One reviewer requested limiting that claim to the
reviewed page and accounting for the mentioned B200 appendix results. We kept
that failed expectation in the first regression artifacts, then created a
separate corrected candidate and fresh run. The production reviewer was not
weakened to pass the test. The two acceptances of the partially corrected version
also show that error detection remains variable: deterministic veto enforcement
cannot guarantee that an LLM notices every factual problem.

One fully corrected review initially assessed extra papers from the review-page
context. Code rejected that role coverage and allowed one schema/reference
repair; the resulting role list contained only the selected TokenWeave paper.
No substantive correction veto was repaired into acceptance.

An independent audit passed for 19 accepted output objects: 50 evidence references
and 77 page references, including support/context partitions, role citation
ownership, original source values/provenance, and artifact/page hashes.
Token totals: 242089 input, 177187 output, including 160550 reported reasoning
tokens. These are isolated process totals, not overlapping component snapshots.

## Artifacts and reproduction

- `/home/hema/research_runs/opportunity_controls_fixed/`: 19 review controls.
- `/home/hema/research_runs/opportunity_fix_regressions/`: nine real-candidate
  repetitions, preserving the partial-correction failure.
- `/home/hema/research_runs/opportunity_fully_corrected/`: three fresh reviews
  of the candidate with both hardware and scope corrected (four calls).
- `/home/hema/research_runs/opportunity_fix_audit.json`: independent audit.

Paid rechecks (use new output directories):

```bash
uv run python -m tools.validate_opportunity_controls --review-only --max-api-calls 28 --out /path/to/new/controls
uv run python -m tools.validate_opportunity_fixes --out /path/to/new/regressions
uv run python -m tools.validate_opportunity_fixes --fully-corrected-only --out /path/to/new/corrected
```

Scientific judgment remains model-dependent. Automatic narrowing/redrafting,
following unreviewed appendix references, semantic deduplication, and the later
novelty-search stage remain separate work. None of the earlier saved outputs
was rewritten or relabelled to hide baseline failures.
