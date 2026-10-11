# Stage 14 — stage 16 publication (`stage16.json`, proposals/, evidence/)

Design reference: architecture §16 Final Candidate Output (hypothesis-level) and §17 Final System Output.

## Aim

Produce the thing a researcher actually reads: one page per hypothesis with
its problem, the direction it belongs to, the hypothesis and test, what the
prior work showed, the critic's verdict, and the evidence behind all of it,
sorted into `approved/`, `pending/` and `discarded/`. The stage runs no model
and no retrieval. Its job is to publish *saved* decisions and to refuse to
publish any decision whose paper trail does not check out — so every file
under `proposals/` can be trusted to be exactly what the reviewed artifacts
say.

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

## Logic

### Step 1 — confirm the two inputs describe the same run

The hypothesis reviews carry the digest of the assessment they were made
from; it must match the assessment inside the critic result. Every
hypothesis in every direction must appear exactly once in the reviews, with
proposal text identical to the direction's. A mismatch means someone is
trying to publish reviews against a different version of the proposals, and
the stage raises. (`stage16`, `publication.py:59-70`.)

### Step 2 — re-derive each status instead of trusting the label

For every row the stage recomputes what the status *should* be from the
saved history, and only then publishes:

- The row's `status` must be one of `approved | pending | discarded`.
- For `approved` and `discarded` rows, the stage re-runs the eligibility
  gate (`critic/hypotheses.eligibility`) and requires it to pass — a
  hypothesis whose coverage was incomplete cannot have been legitimately
  approved. It then checks that the critique attached to the row is either
  the parent direction's published critique or an independent one whose
  packet can be rebuilt byte-for-byte from the critic data; that the last
  review's draft is that critique and its decision was `pass`; that the
  drafts and reviews still validate; and that no `upstream_requests` appear
  anywhere in the history.
- The action rule is applied: `KEEP` or `DOWNRANK` ⇒ `approved`; `DISCARD` ⇒
  `discarded`; anything else ⇒ `pending`. If the saved status disagrees with
  the recomputed one, the stage raises.

This is why a file in `proposals/approved/` can be read as a guarantee: the
code has re-verified the whole chain, not copied a flag. (`publication.py:72-110`.)

### Step 3 — assemble the reading context

Each row is enriched with what a reader needs without opening the JSON:
the direction's title and research question, the problem and gap from the
opportunity, the novelty judgment and its "remaining difference", the
evidence rows and passages the critic saw (`reading_context`), the linked
experiments with the statuses of the sibling hypotheses they depend on, the
critic's verdict and — for `REFINE` — the exact revisions requested, and the
coverage line ("Usable comparisons: 28/30; required coverage: 60%").
(`reading_context`, `publication.py:10`; `saved_metadata`, `:46`.)

### Step 4 — summarise

`counts = {approved, pending, discarded}` and a top-level `status`: `ready`
(some approved, none pending), `partial` (both), `blocked` (only pending),
`empty`. (`publication.py:111-121`.)

### Step 5 — write the report tree

`write_stage16` writes `stage16.json` and hands off to
`research/reading_report.write_reading_report`, which renders one Markdown
and one JSON file per hypothesis into the status folder, an `evidence/`
page per hypothesis, the exported source passages under `evidence/sources/`,
an index `summary.md`, and merges `status`, `hypothesis_counts` and a
hypothesis link index into `summary.json`. (`publication.py:175`;
`reading_report.py:64`.)

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

## Reading a pending page

The "Status and remaining work" section of a `proposals/pending/*.md` file
carries the diagnostic that stopped the hypothesis. Read from the earliest
stage outward:

| Diagnostic | Produced by | What it means |
|---|---|---|
| `Incomplete novelty evidence: ...` | `critic/hypotheses.eligibility` | Too few shortlisted papers were successfully compared (stage 9/10 coverage gate), or the novelty judgment itself was `UNRESOLVED`. |
| `Proposed scientific changes require a fresh novelty check.` | same | The assessment edited the proposal in a way that touched this hypothesis; its novelty must be re-run. |
| `Critic requests REFINE` | `review_hypotheses` | Critique passed review but lists concrete revisions; terminal in the normal pipeline because refinement is disabled. The revisions are printed on the page. |
| `Source defect requires upstream reassessment.` | `ResearchCritic._reviewed` | The critic or its reviewer filed an `upstream_request` — typically a proposal/experiment wording inconsistency — which voids the verdict even if it was `KEEP`. |
| `ValueError: invalid_..._after_repair` / `truncated_output: ...` | `_call` | The model's output could not be made valid, or was cut off. A mechanical failure, not a judgment. |

## Implementation vs design

The design's final output is direction-centric (§17). Stage 16 is an
implementation addition that publishes hypotheses individually so that a
single approved hypothesis is usable even when its direction is `pending`,
and so that every verdict can be audited from the saved evidence files.
