# Stage 14 — stage 16 publication (`stage16.json`, proposals/, evidence/)

Design reference: architecture §16 Final Candidate Output (hypothesis-level) and §17 Final System Output.

## Aim

Publish the saved per-hypothesis decisions as human-readable proposal and
evidence files, without running any model or retrieval. It re-derives each
hypothesis status from the saved critic history and refuses to publish a row
whose lineage does not check out.

## Inputs

Entry points: `stage16(critic, report) -> dict` (`research/publication.py:59`)
and `write_stage16(out, critic, report)` (`:175`). `run_pipeline` calls
`write_stage16(out, state['critic'], json.load(out/'hypothesis_reviews.json'))`
after the portfolio when that file exists (`research/pipeline.py:309`).
Note it always uses the **original** critic checkpoint, not a refined one.

| Input | Contract |
|---|---|
| `critic: CriticResult` | Stage 11. |
| `report: dict` | The `hypothesis_reviews.json` payload; `report['assessment_sha256']` must equal `digest(critic.novelty)`, and the rows must cover every `(direction_id, hypothesis_id)` exactly once with unchanged proposal bytes. |
| `out: Path` | Run directory. |

## Logic (`stage16`, `publication.py:59`)

1. Lineage asserts (above). Harvest paper metadata from the novelty search and critic packets (`saved_metadata`, `:46`).
2. Per hypothesis row:
   - `status` must be `approved | pending | discarded`.
   - For `approved`/`discarded`: `critic.hypotheses.eligibility(...)` must be `None`; the critique must equal the parent direction's published critique **or** the saved packet must equal `packet_for(..., hypothesis_id=...)`; the last review's draft must equal the published critique and its report must be `pass`; drafts/reviews are re-validated; no `upstream_requests` anywhere.
   - Approval rule: `approved` iff action ∈ `{KEEP, DOWNRANK}`; `discarded` iff `DISCARD`; otherwise the row must be `pending` (this is where `REFINE` lands).
   - Attach `direction_title`, `direction_context`, `reading_context` (`:10`, evidence rows and passages the critic saw) and `linked_experiments` (with sibling hypothesis statuses).
3. `counts = {approved, pending, discarded}`; `status`: `partial` if approved and pending both >0, `ready` if approved only, `blocked` if only pending, `empty` otherwise.

`write_stage16` then writes `stage16.json` and calls
`research/reading_report.write_reading_report` (`reading_report.py:64`).

## Outputs

Return value / `out/stage16.json` (`schema_version='stage16_hypotheses_v1'`):

| Field | Contract |
|---|---|
| `status` | `ready | partial | blocked | empty`. |
| `counts` | `{approved, pending, discarded}`. |
| `hypotheses` | Enriched rows: `direction_id`, `hypothesis_id`, `proposal`, `status`, `diagnostic`, `critique`, `reviews`, `packet`, `direction_title`, `direction_context`, `reading_context`, `linked_experiments`. |
| `critic_sha256`, `hypothesis_reviews_sha256` | Lineage digests. |
| `scientific_validation='model_reviewed_not_empirically_validated'`, `automatic_revision=false` | Explicit limits. |

Files written under the run directory:

```
stage16.json, stage16.md                      # result + pointer to summary.md
summary.md                                    # index of all hypotheses
summary.json                                  # merged: status, hypothesis_counts, hypotheses[]
proposals/approved/<hid>.md|.json
proposals/pending/<hid>.md|.json
proposals/discarded/<hid>.md|.json            # created on demand
evidence/<hid>.md                             # evidence the critic saw, per hypothesis
evidence/sources/...                          # exported passages / references
```

`summary.json` after this step: `status` = stage16 status,
`hypothesis_counts` = stage16 counts, `counts` = portfolio counts (unchanged),
`stage16='stage16.json'`, `portfolio='final_portfolio.json'`,
`completed_stages`, `hypotheses` (link index).

## Interpreting a "blocked" summary

`hypothesis_counts.approved == 0` with all rows `pending` means no hypothesis
reached a passing review with `KEEP`/`DOWNRANK`. The `diagnostic` on each
pending row says why, in order of where the pipeline stopped:

| Diagnostic | Produced by | Meaning |
|---|---|---|
| `Incomplete novelty evidence: ...` | `critic/hypotheses.eligibility` | Coverage below `comparison_coverage_threshold` (stage 10). |
| `Proposed scientific changes require a fresh novelty check.` | same | Assessment edits touched this hypothesis; novelty must be re-run. |
| `Critic requests REFINE` | `review_hypotheses` | Critique passed review but asks for revisions; terminal because refinement is disabled. |
| `Source defect requires upstream reassessment.` | `ResearchCritic._reviewed` | Draft or reviewer filed an `upstream_request` (even alongside `KEEP`). |
| `ValueError: invalid_..._after_repair` / `truncated_output: ...` | `_call` | Mechanical failure of the model output. |

## Implementation vs design

The design's final output is direction-centric (§17). Stage 16 is an
implementation addition that publishes hypotheses individually so that a
single approved hypothesis is usable even when its direction is `pending`,
and so that every verdict can be audited from the saved evidence files.
