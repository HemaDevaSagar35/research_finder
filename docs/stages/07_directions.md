# Stage 7 — `directions`

Design reference: architecture §7 Future Direction Generator and §8 Candidate × Hypothesis Interpretation. Implementation notes: `docs/direction_generator.md`.

## Aim

Turn each accepted opportunity into one research direction with testable
hypotheses and linked experiments, grounded only in the opportunity's
evidence and pages. Every draft gets an independent correctness review in a
fresh context; a failing draft may be revised once and re-reviewed. Only
passing drafts become directions. Scientific merit and literature novelty are
explicitly not assessed here.

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

Per opportunity, concurrently (`process`, `generator.py:446`):

1. `_source_context` / `_payload` (`generator.py:169`): rebuild the opportunity's evidence and page inventory; reload papers and pages and compare hashes (`stale_evidence`, `missing_artifact`, `missing_pages`); `len(review_sources) > max_pages` → `page_budget`. The payload carries the full opportunity, the landscape, cross-paper statements (context only, not citable), paper cards, pages, `citable_evidence_ids`, `citable_page_ids`, and the caps.
2. **Generate** (`_generate`, `:325`): task `generate_direction` with `SYSTEM` (`:28`) → `GenerationResponse` (`direction` xor `abstention_reason`); `repair_rounds` format repairs. `_validate_draft` (`:278`): caps, citations ⊆ citable sets, cited pages cover cited evidence papers, at least one supporting paper cited, hypothesis `evidence_ids` ⊆ opportunity evidence.
3. `_assign_ids` (`:350`): `direction_id = f'dir-{index:03d}'` (index in the opportunities list), hypotheses `dir-NNN-hNN`, experiments `dir-NNN-eNN`, experiment `hypothesis_ids` remapped.
4. **Correctness review** (`_review`, `:363`): messages from `directions/judge.py:62`, prompt `judge.SYSTEM` + `test_links.INSTRUCTION`, task `review_direction`, schema `LinkedCorrectnessResponse`, review model. `judge.validate_report` resolves every issue's JSON pointer into the draft and requires quoted grounding text to appear on supplied pages; `validate_links` requires exactly one check per link. Invalid review → `invalid_review` (no repair loop). Each attempt is appended as a `ReviewRecord` with id `dir-NNN-review-<round>`.
5. `_complete` (`:387`): pass → done; abstain → `review_abstained`; revise in round 0 → one `revise_direction` generation with the issues, then round 1 review; revise in round 1 → `correctness_unresolved`.
6. Build `Direction` (`:435`): `direction_id`, `opportunity_id`, `proposal` (the passing `DirectionDraft`), embedded `opportunity` snapshot, `paper_artifact_hashes`, `correctness_review_id`, `context_pages = opportunity.review_sources`, `recommended_next_experiment_id = experiments[0]`, plus fixed flags `correctness_review='passed'`, `hypothesis_status='proposed_untested'`, `scientific_review='not_assessed'`, `literature_novelty='not_assessed'`.

Failures are per-opportunity `Diagnostic`s (reasons in `schemas.py:157`:
`no_opportunities`, `invalid_source`, `missing_artifact`, `stale_evidence`,
`missing_pages`, `page_budget`, `input_budget`, `call_budget`,
`invalid_generation`, `invalid_review`, `review_abstained`,
`correctness_unresolved`, `abstained`, `provider_error`, `call_failed`).

`run_pipeline` stops after this stage if `directions` is empty: status
`blocked` when diagnostics exist, else `empty` (`pipeline.py:291`).

## Outputs

`GenerationResult` — `directions/schemas.py:166` (`schema_version='directions_v3'`).

| Field | Contract |
|---|---|
| `landscape_ref`, `reasoning_ref`, `opportunities_ref` | Input digests. |
| `directions: list[Direction]` | Only proposals whose cited review passed; enforced by `only_reviewed_proposals_advance` (`schemas.py:181`), which also re-validates the saved test-link audit. |
| `reviews: list[ReviewRecord]` | Every review attempt (`review_id`, `round`, `proposal`, `report` xor `error`). |
| `diagnostics: list[Diagnostic]` | Per-opportunity failures. |
| `coverage` | `accepted_opportunities`, `generated_directions`, `opportunities_without_direction`. |
| `calls`, `usage`, `run` | Budget, usage, prompt/model metadata, `correctness_contract='test_links_v1'`, `test_link_reviews`. |

Persisted as `out/directions.json`.

## What later stages rely on

- `direction.proposal.model_dump()` is the `candidate` document for novelty signatures, comparison, assessment and critic; `proposal_paths` / `field_path` values everywhere downstream are JSON pointers into it (e.g. `/hypotheses/2/condition`).
- Novelty targets are `direction_id` plus every `hypothesis_id`.
- `paper_artifact_hashes` and `context_pages` are re-verified by `novelty_search` and `comparison`.
- `recommended_next_experiment_id` and hypothesis↔experiment links are carried into stage 16 as `linked_experiments`.

## Implementation vs design

§7 is implemented; §8 (candidate × hypothesis interpretation) is realised as
the per-hypothesis targets created here and consumed by `novelty_search`.
The test-link audit (`test_links_v1`) is an implementation addition: the
reviewer must state, per hypothesis/experiment pair, whether the experiment's
informative outcomes are consistent with the hypothesis prediction.
