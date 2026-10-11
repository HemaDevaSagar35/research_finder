# Stage 7 — `directions`

Design reference: architecture §7 Future Direction Generator and §8 Candidate × Hypothesis Interpretation. Implementation notes: `docs/direction_generator.md`.

## Aim

An opportunity is a question; a direction is a plan to answer it. For each
accepted opportunity the stage asks a model to write one research direction:
the framing, a proposed mechanism, up to four hypotheses stated so they can
be falsified, and up to four experiments that would test them — all grounded
only in the evidence and pages the opportunity was accepted on. A second
model then reviews the draft in a fresh context for *correctness* (does the
text say what the evidence says? do the experiments actually test the
hypotheses?), and the draft may be revised once. Whether the direction is
scientifically valuable or novel is explicitly not judged here.

## Inputs

Entry point: `DirectionGenerator(store, chat=None, *, settings, provider, model, review_chat=None, review_model).run(landscape, reasoning, opportunities) -> GenerationResult`
— constructor `directions/generator.py:149`, `run` at `:400`. Pipeline:
`DirectionGenerator(self.store, settings=c.directions, **common)` (`research/pipeline.py:171`).

| Input | Contract |
|---|---|
| `landscape`, `reasoning`, `opportunities` | Stages 4–6; `landscape_ref` / `reasoning_ref` must match exactly and the topic must be shared. |
| `settings: Settings` (`generator.py:120`) | `concurrency` 100; `max_calls` None; `max_hypotheses` 4; `max_experiments` 4; `max_pages` 20 (cap on opportunity `review_sources`); `max_input_tokens` / `max_output_tokens` (`DIRECTION_MAX_*`, 500 000); `repair_rounds` 1. |
| provider / models | `DIRECTION_PROVIDER`, `DIRECTION_MODEL`, `DIRECTION_REVIEW_MODEL` fallbacks. |

Draft contract `DirectionDraft` — `directions/schemas.py:45`:

| Field | Contract |
|---|---|
| `title`, `research_direction`, `proposed_mechanism`, `scope`, `assumptions`, `risks`, `uncertainties`, `what_would_falsify_it` | Free text / lists. |
| `rationale: GroundedRationale[]` | Each `statement` with `evidence_ids` and `page_ids` drawn only from the opportunity. |
| `hypotheses: Hypothesis[]` | `hypothesis_id`, `condition`, `intervention`, `expected_effect`, `mechanism`, `assumptions`, `falsification_criterion`, `evidence_ids`. |
| `experiments: Experiment[]` | `experiment_id`, `stage` (`initial|followup`), `hypothesis_ids`, `objective`, `comparison`, `observations`, `why_this_test`, `informative_outcomes`. First experiment must be `initial`; every hypothesis must be linked to ≥1 experiment. |

Review contract: `LinkedCorrectnessResponse` (`directions/test_links.py:16`) =
`CorrectnessResponse` (`decision` `pass|revise|abstain`, `issues:
CorrectnessIssue[]`) + `test_link_checks: TestLinkCheck[]`, one per
(hypothesis, experiment) link; `pass` requires every link `consistent`.

Prompt versions: `direction_v2_explicit_comparisons` (`generator.py:27`),
`direction_correctness_v3_test_links` (`directions/judge.py`).

## Logic

Each opportunity is processed independently and concurrently; a failure in
one never affects the others. (`process`, `generator.py:446`.)

### Step 1 — rebuild and re-verify what the direction may stand on

The code reconstructs the opportunity's evidence inventory (which papers,
which evidence ids, which were `supporting` vs `context_only`) and reloads
every paper and page the opportunity was reviewed on, comparing hashes
against the stage-6 record. The payload sent to the model contains the
opportunity itself, the landscape, the cross-paper statements (as context,
marked *not citable*), the paper cards, the page texts, and two explicit
lists: `citable_evidence_ids` and `citable_page_ids`. Everything the model
is allowed to cite is enumerated up front; everything else is background.
If the pages exceed `max_pages` or any hash has drifted, the opportunity is
withheld (`page_budget`, `stale_evidence`, ...) rather than developed on
evidence that may no longer hold. (`_source_context`, `_payload`,
`generator.py:169`.)

### Step 2 — generate the draft

The system prompt asks for one direction that *attacks* the opportunity —
not one that re-describes the gap. It must propose a mechanism, state each
hypothesis as condition → intervention → expected effect with a
falsification criterion, and design experiments with explicit comparisons
and "which outcome would mean what". The model may abstain with a reason if
the opportunity does not support a direction. (`_generate`, `generator.py:325`;
prompt `SYSTEM`, `:28`; schema `GenerationResponse`.)

The code then checks the draft before anyone reviews it: no more hypotheses
or experiments than allowed; every rationale citation is in the citable
lists; the cited pages cover every paper whose evidence is cited (you may not
cite a paper's evidence without pointing to a page of it); at least one
`supporting` paper is cited; each hypothesis's `evidence_ids` are opportunity
evidence. A failure here is sent back to the model once as a format repair.
(`_validate_draft`, `:278`.)

### Step 3 — give everything a stable id

Hypotheses and experiments are numbered as `dir-NNN-h01`, `dir-NNN-e01`
(NNN = the opportunity's position in the mining result), and the experiments'
`hypothesis_ids` are remapped to the global ids. From here on every later
stage — novelty, critic, stage 16 — refers to a hypothesis by this id and to
a field of the proposal by a JSON pointer into this draft (e.g.
`/hypotheses/2/condition`). (`_assign_ids`, `:350`.)

### Step 4 — independent correctness review

A second model, in a fresh context with the same pages, is asked two things.
First, *grounding*: for each factual statement, does the cited page actually
say it? Issues must quote the page text they rely on, and the code verifies
that each quote appears on a supplied page, so the reviewer cannot
"correct" the draft with facts of its own. Second, *test links*: for every
(hypothesis, experiment) pair, state the hypothesis's prediction, the
experiment's informative outcomes, and whether they are consistent — the
check that an experiment really tests the hypothesis it claims to, including
the equality / null case. The review passes only if every link is
`consistent` and there are no grounding issues. An invalid review (missing a
link, unresolvable pointer) is a failure of the review, not of the draft, and
is recorded as `invalid_review`. (`_review`, `generator.py:363`;
`directions/judge.py`; `directions/test_links.py`.)

### Step 5 — at most one revision

`pass` → the draft becomes a direction. `revise` in the first round → the
draft and the reviewer's issues go back to the generator once
(`revise_direction`), and the revised draft is reviewed afresh. `revise`
again, or `abstain` → the opportunity ends with a diagnostic
(`correctness_unresolved`, `review_abstained`). Two rounds is the limit so
that a draft cannot be nudged through review by repeated small edits.
(`_complete`, `:387`.)

### Step 6 — publish with explicit non-claims

The resulting `Direction` carries the passing proposal, the opportunity
snapshot, the artifact hashes it was built on, the id of the review that
passed it, and fixed flags: `correctness_review='passed'`,
`hypothesis_status='proposed_untested'`, `scientific_review='not_assessed'`,
`literature_novelty='not_assessed'`. Those flags are how the output states
what has and has not been checked at this point.

If no opportunity yields a direction, `run_pipeline` stops here with status
`blocked` (diagnostics exist) or `empty` (`pipeline.py:291`).

## Outputs

`GenerationResult` — `directions/schemas.py:166` (`schema_version='directions_v3'`).

| Field | Contract |
|---|---|
| `landscape_ref`, `reasoning_ref`, `opportunities_ref` | Input digests. |
| `directions: list[Direction]` | Only proposals whose cited review passed; enforced by `only_reviewed_proposals_advance` (`schemas.py:181`), which also re-validates the saved test-link audit. |
| `reviews: list[ReviewRecord]` | Every review attempt (`review_id`, `round`, `proposal`, `report` xor `error`). |
| `diagnostics: list[Diagnostic]` | Per-opportunity failures (`schemas.py:157`). |
| `coverage` | `accepted_opportunities`, `generated_directions`, `opportunities_without_direction`. |
| `calls`, `usage`, `run` | Budget, usage, prompt/model metadata, `correctness_contract='test_links_v1'`, `test_link_reviews`. |

Persisted as `out/directions.json`.

## What later stages rely on

- `direction.proposal.model_dump()` is the `candidate` document for every later stage; `proposal_paths` / `field_path` values are JSON pointers into it.
- Novelty targets are `direction_id` plus every `hypothesis_id` — the direction as a whole and each hypothesis are assessed separately.
- `paper_artifact_hashes` and `context_pages` are re-verified by `novelty_search` and `comparison`.
- The hypothesis↔experiment links and `recommended_next_experiment_id` are carried into stage 16 as `linked_experiments`.

## Implementation vs design

§7 is implemented; §8 (candidate × hypothesis interpretation) is realised as
the per-hypothesis targets created here and consumed by `novelty_search`.
The test-link audit (`test_links_v1`) is an implementation addition.
