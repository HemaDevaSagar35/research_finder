# Stage 6 — `opportunities`

Design reference: architecture §6 Opportunity Miner. Implementation notes: `docs/opportunity_miner.md`.

## Aim

Turn what the corpus has established into what it has left open. Given the
verified findings, observations and tensions, the stage proposes unresolved
research questions — a limitation several papers share, an assumption nobody
relaxes, a regime nobody tests, a contradiction nobody explains — and then
checks each proposal against the original paper pages: is the claimed gap
really there, or does one of the papers already address it? Only proposals
that survive that check become opportunities. The result is scoped to the
reviewed corpus; whether the wider literature has already filled the gap is a
question for the novelty stages.

## Inputs

Entry point: `OpportunityMiner(store, chat=None, *, settings, provider, model, review_model).run(landscape, reasoning) -> MiningResult`
— constructor `opportunities/miner.py:119`, `run` at `:312`. Pipeline:
`OpportunityMiner(self.store, settings=c.opportunities, **common)`
(`research/pipeline.py:169`).

| Input | Contract |
|---|---|
| `landscape: Landscape` | Stage 4; context items and `paper_ids`. |
| `reasoning: CrossPaperReasoning` | Stage 5; `landscape_ref` and `topic` must match the landscape exactly, else `ValueError`. Only `verified` items with `review_sources` enter. |
| `settings: Settings` (`miner.py:82`) | `concurrency` 100; `max_calls` None; `batch_size` 0 (= one batch); `max_batches` 8; `max_candidates_per_batch` 5 (pipeline default overrides to **8**, `pipeline.py:57`); `max_review_pages` 20; `max_input_tokens` / `max_output_tokens` (`OPPORTUNITY_MAX_*`, 500 000); `max_review_output_tokens` (`OPPORTUNITY_REVIEW_MAX_OUTPUT_TOKENS`, else `REASON_MAX_REVIEW_OUTPUT_TOKENS`); `repair_rounds` 1. |
| provider / models | `OPPORTUNITY_PROVIDER`, `OPPORTUNITY_MODEL`, `OPPORTUNITY_REVIEW_MODEL` fallbacks. |

`PROMPT_VERSION = 'opportunity_v2_review_roles'` (`miner.py:29`).

## Logic

### Step 1 — fix the evidence universe

Before any model call the code decides exactly what may be cited. Every
verified finding, observation and tension becomes a *source* with an id like
`finding:t002-f01`; every piece of evidence those sources rest on is
re-labelled under its source, `observation:t002-o01/t002-e001`. Diagnostics
from stage 5 are excluded on purpose: a statement the reviewer rejected is
not a fact about the corpus and must not motivate a gap. The lineage hash of
the landscape is checked so that the reasoning being mined is the reasoning
built from *this* landscape. (`_inputs`, `miner.py:194`.)

### Step 2 — propose

The sources are sent to the model (by default all in one batch) together
with the landscape items they refine and the concept groups involved. The
prompt asks for up to N candidate opportunities, each with: a *category*
from a fixed list (recurring limitation, restrictive assumption, missing
regime, contradiction, failure mode, missing evaluation, unresolved
trade-off, mechanistic interaction), the open question, why it is open, its
scope, acknowledged uncertainties, and — critically — which source ids and
which evidence ids motivate it. The model is told that an empty list is a
valid answer: if the corpus has no real gap, inventing one is the failure
mode. (`_json('proposal', ...)`, `miner.py:160`; prompt `PROPOSE`, `:30`;
schema `Proposal` / `Candidate`, `opportunities/schemas.py:19`.)

### Step 3 — filter mechanically

Each candidate gets an id `op-<batch>-<n>`. Then the code throws out, with a
diagnostic, any candidate that cites a source outside its batch, cites
evidence that does not belong to the sources it names, or repeats sources or
evidence (`invalid_reference`), and any candidate whose question — lower-cased
and whitespace-normalised — plus its source set duplicates an earlier one
(`duplicate`). These are cheap checks that catch the common ways a model
"supports" a question with evidence that is not about it.
(`process_batch`, `miner.py:362`.)

### Step 4 — reconstruct the pages and re-verify the evidence

For each surviving candidate the code gathers the pages that the cited
sources were verified against in stage 5 plus the pages the cited evidence
points to, reloads the papers and pages, and compares hashes and values
against what stage 5 recorded. If anything moved — a re-extracted
`paper.json`, a changed page file, a location that cannot be resolved — the
candidate is withheld (`stale_evidence`, `missing_artifact`, `missing_pages`,
`unresolved_location`) rather than reviewed against evidence that no longer
says what it did. If the page set exceeds `max_review_pages` the candidate
is withheld as `page_budget`: a review of *some* of the pages would not be a
review of the claim. (`_review`, `miner.py:232`.)

### Step 5 — review against the pages

A second model (the review model) receives the candidate and the actual page
text and must answer: does the evidence, as written on these pages, support
the claim that this question is unresolved in these papers? It returns a
decision (`accept | reject | insufficient | revise`), a factual status
(`supported | needs_correction | uncertain`), any corrections it believes the
proposal needs, and — new in v2 — a *role* for each paper it looked at:
`supporting` (this paper's evidence motivates the gap) or `context_only`
(relevant but does not establish the gap). (`_json('review', ...)`; prompt
`REVIEW`, `miner.py:46`; schema `Decision`, `schemas.py:41`.)

### Step 6 — code decides what "accept" is allowed to mean

The model's `accept` is necessary, not sufficient. The code vetoes it when
the reviewer also asked for corrections or said the facts need correction
(`requires_correction`) — an accepted-but-needs-fixing proposal would carry a
known defect into direction generation — and downgrades an `accept` whose
factual status is `uncertain` to `insufficient`. An accepted candidate must
also cite only pages it was shown, give exactly one role per evidence paper,
and have at least one `supporting` paper; otherwise the acceptance is invalid.
What passes becomes an `Opportunity` with its roles, evidence and the exact
page hashes it was reviewed on. Everything else is a `Diagnostic` with a
reason; no per-candidate failure aborts the stage. (`miner.py:177-184`,
`:279-310`.)

## Outputs

`MiningResult` — `opportunities/schemas.py:86` (`schema_version='opportunities_v2'`).

| Field | Contract |
|---|---|
| `landscape_ref`, `reasoning_ref` | `{schema_version, sha256}` of the inputs. |
| `opportunities: list[Opportunity]` | Accepted only. |
| `diagnostics: list[Diagnostic]` | `candidate_id`/`batch`, `reason`, `detail` for every withheld or failed item. |
| `coverage` | `reviewed_sources`, `batches`, `proposed`, `accepted`. |
| `calls`, `usage`, `run` | Budget snapshot, token usage, provider/model/settings metadata. |

`Opportunity` (`schemas.py:57`): `opportunity_id`, `candidate`, `sources:
SourceReference[]`, `paper_ids`, `supporting_paper_ids`, `context_paper_ids`,
`paper_assessments`, `support` (`single_paper|multiple_papers`), `evidence:
Evidence[]`, `review_sources: ReviewSource[]`, `review_notes`,
`verification='supported_in_reviewed_corpus'`, `literature_novelty='not_assessed'`.

Persisted as `out/opportunities.json`.

## What later stages rely on

- `directions` develops exactly the accepted `opportunities`; an opportunity's `evidence` and `review_sources` are the *only* things a direction's rationale may cite, and its `supporting_paper_ids` must be cited at least once.
- `opportunity_id` becomes `Direction.opportunity_id`; the enumeration index becomes the `dir-NNN` number.
- `support = single_paper` is carried forward as a visible caveat: a gap motivated by one paper is weaker than one several papers share.

## Implementation vs design

§6 is implemented with one addition: the second "review against pages" call
with supporting/context paper roles (prompt version `review_roles`), which the
design only implied. Literature novelty is explicitly deferred to §9–14.
