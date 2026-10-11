# Stage 6 — `opportunities`

Design reference: architecture §6 Opportunity Miner. Implementation notes: `docs/opportunity_miner.md`.

## Aim

Propose unresolved research questions from the verified cross-paper
findings, observations and tensions, then accept only those whose motivating
claims hold when checked against the original paper pages. The output is
corpus-scoped ("supported in the reviewed corpus"); it does not certify
literature-wide novelty and it does not generate hypotheses.

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

LLM I/O models (`opportunities/schemas.py`): `Candidate` (`:19`) —
`category` (one of eight in `Category`, `:8`), `question`, `rationale`,
`scope`, `uncertainties`, `source_ids` (≥1), `evidence_ids` (≥1);
`Proposal{candidates}`; `Decision` (`:41`) — `candidate_id`, `decision`
(`accept|reject|insufficient|revise`), `notes`, `page_ids`, `factual_status`
(`supported|needs_correction|uncertain`), `required_corrections`,
`paper_assessments: PaperAssessment[]` (`paper_id`, `role`
`supporting|context_only`, `rationale`, `evidence_ids`, `page_ids`).

`PROMPT_VERSION = 'opportunity_v2_review_roles'` (`miner.py:29`).

## Logic

1. `_inputs` (`miner.py:194`): verify lineage; build the landscape item index; for each verified finding/observation/tension mint `source_id = '<kind>:<id>'` and namespace its evidence as `'<source_id>/<evidence_id>'`; set `origin` from the limitation value path.
2. Batch the sources (`batch_size` 0 → all in one batch); batches beyond `max_batches` are reported as `batch_limit` diagnostics, not silently dropped (`miner.py:323`).
3. **Propose** per batch (`_json('proposal', ...)`, `miner.py:160`): prompt `PROPOSE` (`miner.py:30`) → `Proposal`; one repair round on validation failure; input over `max_input_tokens` → `input_budget` failure. More than `max_candidates_per_batch` candidates is a validation error.
4. Deterministic filtering (`process_batch`, `miner.py:362`): assign `op-<batch:03d>-<cand:03d>`; drop candidates whose `source_ids`/`evidence_ids` are unknown, duplicated, or whose evidence does not belong to the cited sources (`invalid_reference`); dedup on `(normalised question, sorted source_ids)` (`duplicate`).
5. **Review** per surviving candidate (`_review`, `miner.py:232`):
   - Collect pages from the cited sources' `review_sources` and evidence `source_locations`; reload artifacts and pages and compare hashes / values (`stale_evidence`, `missing_artifact`, `missing_pages`, `unresolved_location`); more than `max_review_pages` → `page_budget` (no partial review).
   - LLM `review` with prompt `REVIEW` (`miner.py:46`) → `Decision`, review model.
   - Code vetoes before acceptance (`miner.py:177`): any `required_corrections`, `factual_status='needs_correction'` or `decision='revise'` → `requires_correction`; `accept` with `factual_status='uncertain'` → `insufficient`.
   - An accepted decision must cite only supplied pages, give exactly one assessment per evidence paper, and name at least one `supporting` paper (`miner.py:279`).
6. Build `Opportunity` for accepted candidates; everything else becomes a `Diagnostic` with a reason. Per-item failures never raise out of `run`; only lineage/constructor errors do.

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

- `directions` develops exactly the accepted `opportunities` list and re-checks `landscape_ref` / `reasoning_ref`; the embedded `evidence` and `review_sources` define what a direction may cite.
- `opportunity_id` becomes `Direction.opportunity_id`; the enumeration index becomes the `dir-NNN` number.

## Implementation vs design

§6 is implemented with one addition: the second "review against pages" call
with supporting/context paper roles (prompt version `review_roles`), which the
design only implied. Literature novelty is explicitly deferred to §9–14.
