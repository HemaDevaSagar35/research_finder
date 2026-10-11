# Stage 9 — `comparison`

Design reference: architecture §12 Candidate-vs-Prior-Work Comparison. Implementation notes: `docs/novelty_search.md`.

## Aim

For every shortlisted prior paper, read it and decide how it relates to each
target: does it already do what the hypothesis proposes, overlap partly,
supply only components, or barely touch it? The stage insists on doing this
from the paper's own pages, in two separable steps — first establish *what
the paper says* as a set of audited claims, then *interpret* those claims
against the candidate — so that an interpretation can never rest on a claim
the paper does not make. Its output is pairwise relationships; the overall
novelty judgment is left to `assessment`.

## Inputs

Entry point: `NoveltyComparator(store, chat=None, *, review_chat, provider, model, review_model, settings).run(generation, search, *, paper_ids=None, recovery=None) -> NoveltyComparisonResult`
— constructor `novelty/comparison.py:57`, `run` at `:210`. Pipeline:
`NoveltyComparator(self.store, settings=c.comparison, **common).run(state['directions'], state['novelty_search'])`
(`research/pipeline.py:178`).

| Input | Contract |
|---|---|
| `generation`, `search` | Stages 7–8; `search.directions_ref` must match the generation and each `candidate_sha256` must match. |
| `paper_ids: set[str] \| None` | Optional scope (recovery only): subset of shortlisted papers to (re)compare; out-of-scope papers keep their saved result or are `skipped`. |
| `recovery` | Prior `NoveltyComparisonResult` to reuse completed papers and saved evidence audits. |
| `settings: Settings` (`comparison.py:26`) | `concurrency` 100; `max_calls` None; `max_input_tokens` / `max_output_tokens` (`NOVELTY_MAX_*`, 500 000). |

Prompt versions (`novelty/comparison_prompts.py`): `VERSION =
'novelty_comparison_v7_targeted_repair'`, `REVIEW_VERSION =
'novelty_comparison_review_v7_addressed'`, `EVIDENCE_VERSION =
'novelty_evidence_v3_targeted_repair'`.

## Logic

### The unit of evidence: passages

Each prior paper's pages are cut into **passages**, and a passage id encodes
the page, the exact character span and a hash of the text
(`41d503b70097#p8:3f9a2c1b7e4d`). Every claim in this stage cites passage
ids, never free text, so a citation can always be re-opened to the exact
words it rests on, and a changed page invalidates it automatically. On the
wire the ids are replaced by short aliases (`src0`, `src1`, ...) — models
copy 12-hex hashes unreliably — and restored on the way back. (`Passage`,
`novelty/comparison_schemas.py:14`; `AliasedCalls`, `novelty/comparison_aliases.py`.)

### Per direction: shortlist and sanity check

The ranked papers of all the direction's targets are merged into one
shortlist, so a paper that surfaced for `h01` and `h03` is read once and
compared against both. The direction's context is rebuilt and its signature
re-validated; if that fails, every paper in the shortlist is marked `failed`
— the comparison cannot be trusted if the thing being compared has drifted.
(`process`, `comparison.py:245`.)

### Per paper, phase 1 — establish what the paper says

1. **Extract.** The model reads the paper's passages and, for each target,
   writes *claims*: short factual statements about what the paper does,
   studies or reports, each citing the passages that state it; a *coverage*
   judgment of whether the passages shown are enough to characterise the
   paper for that target; and a provisional *relationship* per target. It
   is also handed the signature's `proposal_requirements` so it knows which
   aspects of the paper matter. (`EvidenceBuilder.generate`,
   `novelty/comparison_workflow.py:202`; prompt `EXTRACT`.)
2. **Audit.** A second model, with only the passages and the claims, checks
   each claim: `supported` by its citations, `unsupported`, or
   `missing_citation` (true but the citing passage is elsewhere). It also
   checks whether the per-target coverage is adequate. (`EvidenceBuilder.audit`,
   `:251`; prompt `EVIDENCE_REVIEW`; schema `EvidenceReport`.)
3. **Repair what the audit flagged.** Claims the auditor rejected go back to
   the extractor with the auditor's notes, up to three times. Each cycle is
   recorded as an `EvidenceReview` so the full history is visible. The loop
   stops early if a repair reproduces a previous attempt — a sign the model
   is going in circles. (`repair_remaining`, `:277`; `patch`, `:223`.)

### The eligibility gate

Before any interpretation happens, the code decides which targets have
*usable* evidence: a target is eligible when its claims are `supported`,
its coverage is `adequate`, and its provisional relationship refers only to
supported claims. One subtlety carries a history. The auditor sometimes
cites an extra passage that is not attached to the claim. If the auditor
says the claim is `missing_citation`, or the claim is not supported, or the
auditor cited *none* of the claim's own passages, that extra citation is a
real gap and blocks eligibility. But if a supported claim's supported check
merely cites its own passages *plus* a corroborating one, that is extra
support, not a defect — treating it as a defect used to send the same record
back for re-audit three times, each audit naming new passages, and never
converging (`docs/implementation_status.md`, 2026-10-11). If no target is
eligible the paper ends `unresolved`. (`eligible`, `workflow.py:100`;
`scope_citation_gaps`, `:70`; `corroborating_citations`, `:89`.)

### Per paper, phase 2 — interpret and review

4. **Compare.** The model now sees the *audited* claims (not the raw pages)
   and the candidate, and for each eligible target states the relationship —
   e.g. direct empirical study of the same question, partial overlap,
   components only, adjacent — the finding, and which claims the judgment
   rests on. It may ask for specific additional evidence
   (`evidence_requests`), which reopens phase 1 once. The code binds the
   relationship to the claims and the hypothesis it tests itself, so a
   comparison cannot quietly cite a claim that was not supported.
   (`_reviewed`, `comparison.py:64`; prompt `COMPARE`; `assemble`,
   `bind_relationships`, `workflow.py:109-135`.)
5. **Review.** An independent reviewer gets the claims, the passages and the
   comparison, and asks: does the relationship follow from these claims, is
   anything overstated, is a claim being used for more than it says? `pass`
   publishes; `revise` either reopens the evidence (if the problem is a
   claim) or asks for an interpretation patch (if the problem is the
   reading), for one more round; `abstain` or a second failure publishes
   only the pairs the reviewer did accept. (prompt `REVIEW`; schema
   `ComparisonReport`; `accepted_targets`, `project`, `workflow.py:136-158`.)

### Status and what it means

`complete` — every requested target has an accepted pair. `partial` — some
do. `unresolved` — the paper was read and audited but nothing was
publishable. `failed` — the paper could not be loaded or compared at all.
`skipped` — outside a recovery scope with no saved result. Accepted pairs
are never overwritten by a later worse attempt, and a truncated model
response (`truncated_output: ...`) is recorded as the diagnostic rather than
retried with the same oversize input. (`_paper`, `comparison.py:159`;
`one`, `:281`.)

### Recovery

When replaying from a saved run, a paper whose passages are unchanged reuses
its last saved evidence audit and starts at the eligibility gate — the
expensive extract/audit phase is not repeated. A paper whose target set has
grown (a retrieval that failed before now succeeded) is compared afresh.
(`reusable_evidence`, `comparison.py:142`.)

## Outputs

`NoveltyComparisonResult` — `novelty/comparison_schemas.py:297` (`schema_version='novelty_comparison_v6'`).

| Field | Contract |
|---|---|
| `directions_ref`, `search_ref` | Input digests. |
| `candidates: list[CandidateComparison]` | Per direction: `direction_id`, `candidate_sha256`, `papers: PaperComparison[]`, `diagnostic`. |
| `calls`, `usage`, `run` | Budget snapshot; usage incl. `truncated` count; prompt versions, settings, `selected_papers`, `scope`, `repair_policy`, `recovery_parent_sha256`. |
| `literature_novelty='not_assessed'` | |

`PaperComparison` (`:189`): `paper_id`, `targets`, `passages`,
`evidence_reviews: EvidenceReview[]` (the full phase-1 history),
`reviews: ComparisonReview[]` (phase 2), `comparison` (the published,
projected result), `status`, `diagnostic`. Validators enforce that the
published comparison equals the projection of the last passing review.

Persisted as `out/comparison.json` (or `comparison_recovery_N.json` under recovery).

## What later stages rely on

- `assessment` counts a (target, paper) pair as *reviewed* only when there is an accepted pair for that target whose relationship is not `insufficient_evidence`; everything else lowers that target's coverage.
- The accepted claims and their passages are the only evidence the assessor and critic may cite; the text travels forward verbatim.
- `research/recover.py --direction/--paper` narrows `paper_ids`; saved results outside the scope are preserved.

## Implementation vs design

§12 describes a single compare step. The implementation splits it into
extract → audit → repair → compare → review, with code-owned binding of
hypothesis/claim ids, so that a model may not cite evidence the auditor did
not support.
