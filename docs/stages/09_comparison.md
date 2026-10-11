# Stage 9 — `comparison`

Design reference: architecture §12 Candidate-vs-Prior-Work Comparison. Implementation notes: `docs/novelty_search.md`.

## Aim

For every shortlisted (target, prior paper) pair, decide how the prior paper
relates to the candidate using the paper's own pages: first extract and audit
the evidence claims, then compare, then have an independent reviewer check
the comparison. The stage publishes pairwise overlap relationships; it does
not decide literature-wide novelty (`literature_novelty='not_assessed'`).

## Inputs

Entry point: `NoveltyComparator(store, chat=None, *, review_chat, provider, model, review_model, settings).run(generation, search, *, paper_ids=None, recovery=None) -> NoveltyComparisonResult`
— constructor `novelty/comparison.py:57`, `run` at `:210`. Pipeline:
`NoveltyComparator(self.store, settings=c.comparison, **common).run(state['directions'], state['novelty_search'])`
(`research/pipeline.py:178`). Internally the comparator wraps a
`NoveltySearcher` for its `_call` plumbing inside `AliasedCalls`
(`novelty/comparison_aliases.py`).

| Input | Contract |
|---|---|
| `generation`, `search` | Stages 7–8; `search.directions_ref` must match the generation and each `candidate_sha256` must match. |
| `paper_ids: set[str] \| None` | Optional scope (recovery only): subset of shortlisted papers to (re)compare; out-of-scope papers keep their saved result or are `skipped`. |
| `recovery` | Prior `NoveltyComparisonResult` to reuse completed papers and saved evidence audits. |
| `settings: Settings` (`comparison.py:26`) | `concurrency` 100; `max_calls` None; `max_input_tokens` / `max_output_tokens` (`NOVELTY_MAX_*`, 500 000). |

Core contracts (`novelty/comparison_schemas.py`, `novelty/comparison_records.py`):

| Model | Contract |
|---|---|
| `Passage` (`comparison_schemas.py:14`) | `passage_id = '<paper>#p<N>:<12 hex>'` (hash of page sha, offsets and text), `page_id`, `page_sha256`, `start`, `end`, `text`. Immutable quote unit for all citations. |
| `EvidenceResponse` (`comparison_records.py:80`) | `record` (claims per target with `passage_ids`, coverage per target, relationships) xor `abstention_reason`; `revision_notes`. |
| `EvidenceReport` (`:115`) | Auditor output: `claim_checks` (`supported|unsupported|missing_citation|...` per claim), `coverage_checks`, `summary`. |
| `EvidenceReview` (`:127`) | One audit cycle: `version` 0–7, `trigger` `initial|evidence_repair|comparison_reopen`, `record`, `report` xor `error`, `format_repairs`, `model`. |
| `ComparisonResponse` / `ComparisonReport` | COMPARE draft (per-target pairs with relationship, finding, cited claims) and its REVIEW report. |
| `PaperComparison` (`:189`) | Per paper: `targets`, `passages`, `evidence_reviews`, `reviews`, published `comparison`, `status`, `diagnostic`. Validators enforce that the published comparison equals the projection of the last passing review. |

Prompt versions (`novelty/comparison_prompts.py`): `VERSION =
'novelty_comparison_v7_targeted_repair'`, `REVIEW_VERSION =
'novelty_comparison_review_v7_addressed'`, `EVIDENCE_VERSION =
'novelty_evidence_v3_targeted_repair'`.

## Logic

Per direction (`process`, `comparison.py:245`): group ranked papers by
target, build the shortlist, re-run `_context` and `validate_signature`; if
that fails every paper is `failed`. Otherwise, per paper concurrently
(`one`, `:281` → `_paper`, `:159`):

1. Load the paper and its pages (`page_numbers`, `:33`) and cut them into `Passage`s. Payload: targets, `proposal_requirements` (from the signature), candidate passages, prior passages (aliased to `src0, src1, ...` on the wire by `AliasedCalls`; ids restored on return), table references, missing pages.
2. Recovery: `reusable_evidence(old, passages)` (`:142`) reuses the last saved audit when the passages are identical and its eligible targets include something still unresolved; if the saved paper's targets differ from the new grouping, the paper is compared afresh.
3. **Evidence** (`EvidenceBuilder.initial`, `novelty/comparison_workflow.py:296`): `generate` (task `extract_comparison_evidence`, prompt `EXTRACT` → `EvidenceResponse`, 1 repair) → `audit` (task `review_comparison_evidence`, prompt `EVIDENCE_REVIEW` → `EvidenceReport`) → `repair_remaining` (up to 3 `evidence_repair` cycles via `patch`, task `patch_comparison_evidence`, stopping when a repair repeats a fingerprint).
4. **Eligibility** (`eligible`, `workflow.py:100`): a target is eligible when its claims are `supported`, have no `scope_citation_gaps` (`workflow.py:70`: an auditor scope check cites passages not attached to the claim **and** either the check is `missing_citation`, the claim is not `supported`, or the check cites none of the claim's own passages), coverage is adequate, and relationship references point at supported claims. `corroborating_citations` (`:89`) reports extra citations on supported claims without blocking. No eligible target → the paper is `unresolved`.
5. **Compare** (`_reviewed`, `comparison.py:64`, ≤2 rounds): task `compare_prior_work` with `COMPARE` → `ComparisonResponse` (or `patch_comparison` with `PATCH_INTERPRETATION` when revising sparsely); 1 format repair; `assemble` + `bind_relationships` (`workflow.py:109-135`) bind `hypothesis_tested` and claim ids from code, not from the model; an `evidence_requests` list reopens the evidence once.
6. **Review**: task `review_comparison` with `REVIEW` → `ComparisonReport`; `validate_report`. `pass` → `project(draft, accepted_targets(...))` becomes the published `comparison`; `revise` → reopen evidence (claim issues) or patch interpretation, one more round; `abstain` / second failure → keep accepted pairs only.
7. Status: `complete` (all requested targets accepted), `partial` (some), `unresolved` (reviews ran, nothing publishable), `failed` (load/context failure or exception with empty history), `skipped` (out of `paper_ids` scope with no saved result). Accepted pairs are never overwritten by a later regression.

Truncated model output raises `TruncatedOutputError` and is not resent with
the same input; the paper ends `unresolved`/`failed` with a
`truncated_output: ...` diagnostic.

## Outputs

`NoveltyComparisonResult` — `novelty/comparison_schemas.py:297` (`schema_version='novelty_comparison_v6'`).

| Field | Contract |
|---|---|
| `directions_ref`, `search_ref` | Input digests. |
| `candidates: list[CandidateComparison]` | Per direction: `direction_id`, `candidate_sha256`, `papers: PaperComparison[]`, `diagnostic`. |
| `calls`, `usage`, `run` | Budget snapshot; usage incl. `truncated` count; prompt versions, settings, `selected_papers`, `scope`, `repair_policy`, `recovery_parent_sha256`. |
| `literature_novelty='not_assessed'` | |

Persisted as `out/comparison.json` (or `comparison_recovery_N.json` under recovery).

## What later stages rely on

- `assessment` counts a (target, paper) pair as `reviewed` only when the paper has an accepted pair for that target and its relationship is not `insufficient_evidence`; everything else is `withheld`/`skipped`/`failed` and lowers coverage.
- Accepted claims and their `passage_ids` are the only evidence the assessor and critic may cite; passage text is carried forward verbatim.
- `research/recover.py --direction/--paper` narrows `paper_ids`; saved results outside the scope are preserved.

## Implementation vs design

§12 describes a single compare step. The implementation splits it into
extract → audit → repair → compare → review, with code-owned binding of
hypothesis/claim ids, so that a model may not cite evidence that the auditor
did not support. The October 2026 fix narrowed `scope_citation_gaps` so that
corroborating citations on supported claims no longer block eligibility
(`docs/implementation_status.md`, 2026-10-11).
