# Stage 8 — `novelty_search`

Design reference: architecture §9 Novelty Search = Second RAG, §10 Novelty Signature, §11 Second RAG Retrieval. Implementation notes: `docs/novelty_search.md`.

## Aim

For every direction and every hypothesis, build a reviewed *signature* (what
exactly is being claimed, split into source facts and candidate proposals,
with provenance) and a set of search queries; then retrieve candidate prior
papers from the full index and rerank them. The stage produces shortlists
only — `novelty='not_assessed'` is stamped on every output.

## Inputs

Entry point: `NoveltySearcher(store, retriever, chat=None, *, review_chat, provider, model, review_model, settings, corpus_id, retrieval_mode).run(generation, *, extra_pages=None, signature_recovery=None, recovery=None) -> NoveltySearchResult`
— constructor `novelty/pipeline.py:146`, `run` at `:350`. Pipeline
(`research/pipeline.py:174`): `NoveltySearcher(self.store, self.get_retriever(),
settings=c.novelty_search, corpus_id=<index_dir or 'configured-opensearch'>,
retrieval_mode=c.backend, **common).run(state['directions'])`.

| Input | Contract |
|---|---|
| `generation: GenerationResult` | Stage 7; direction ids must be unique. |
| `retriever: MultiQueryRetriever` | Same backend as stage 2. |
| `settings: Settings` (`pipeline.py:71`) | `concurrency` 100; `max_calls` None; `max_queries` 4 per target; `record_k` 50; `candidate_k` 100 (papers retrieved); `rerank_k` 20 (papers kept); `retrieval_timeout` 120; `max_input_tokens` / `max_output_tokens` (`NOVELTY_MAX_INPUT_TOKENS` / `NOVELTY_MAX_OUTPUT_TOKENS`, default 500 000). |
| provider / models | `NOVELTY_PROVIDER`, `NOVELTY_MODEL`, `NOVELTY_REVIEW_MODEL` fallbacks. |
| `recovery`, `signature_recovery` | Prior results for resumption (used by `research/recover.py`). |

Signature contract (`novelty/schemas.py`):

| Model | Contract |
|---|---|
| `Facet` (`:11`) | `text`; `basis` `source_fact|candidate_proposal|unknown`; `proposal_paths` (JSON pointers into the proposal; required for `candidate_proposal`); `evidence_ids`; `source_spans` (page quotes; required for `source_fact`). |
| `TargetSignature` (`:32`) | `target_id`, `level` `direction|hypothesis`; facets `problem`, `intervention`, `expected_effect` (≥1 each), `decision_signal`, `mechanism`, `regime`, `comparison`; `queries` (1–20). |
| `SignatureResponse` (`:49`) | `signature: SignatureDraft` xor `abstention_reason`. |
| `RankingResponse` (`:79`) | `papers: RankedMatch[]` (`paper_id`, `why_relevant`). |

Prompt versions (`novelty/prompts.py`): `SIGNATURE_VERSION =
'novelty_signature_v4_direct_page_provenance'`, plus `REVIEW_VERSION`, `RANK_VERSION`.

## Logic

Per direction, concurrently (`process`, `pipeline.py:384`):

1. `_context` (`:211`): reload the direction's papers and context pages, verify `paper_artifact_hashes` and the opportunity evidence, and build the payload `{candidate, opportunity, paper_artifacts, pages, max_queries}` with `page_id = '<paper>#p<N>'`.
2. **Signature** (`_signature`, `:261`): task `create_signature` with `prompts.SIGNATURE` → `SignatureResponse`; one format repair (`repair_signature`); `normalize_proposal_paths` then `validate_signature` (`:113`): one target per direction + hypothesis, unique queries ≤ `max_queries`, pointers resolve, quotes present on the cited pages and covering every cited paper. Then up to two review rounds: task `review_signature` with `prompts.REVIEW` → `CorrectnessResponse`; `pass` → accept; `revise` → `revise_signature` once; `abstain` or second non-pass → `signature_review_unresolved`. Each round is kept as a `SignatureReview`.
3. **Search** per target (`_search`, `:318`): `retriever.retrieve(QueryPlan(queries), record_k, paper_k=candidate_k)`; load every returned artifact (unloadable → `unavailable_papers`); task `rerank` with `prompts.RANK` → `RankingResponse`, which must return exactly `min(rerank_k, loaded)` unique loaded ids. Status `complete` (all loaded), `partial` (some unavailable) or `failed` (exception, with `diagnostic`).
4. Any failure before a signature is accepted yields a `CandidateSearch` with `signature=None` and a `diagnostic`; no searches run for that direction.

Shared call plumbing (`_call`, `:165`): JSON body with `task`; input budget
check; `TruncatedOutputError` if the provider reports truncation (not
repaired); `drop_null_extras` (`:34`) removes unknown keys whose value is
`null` and records the normalisation; review tasks use the review model.

## Outputs

`NoveltySearchResult` — `novelty/schemas.py:143` (`schema_version='novelty_search_v1'`).

| Field | Contract |
|---|---|
| `directions_ref` | `{schema_version, sha256}` of the generation. |
| `candidates: list[CandidateSearch]` | One per direction. |
| `calls`, `usage`, `run` | Budget snapshot, usage delta, provider/models/prompt versions/settings/`corpus_id`/`retrieval_mode`. |
| `novelty='not_assessed'` | Explicit non-verdict. |

`CandidateSearch` (`schemas.py:110`): `direction_id`, `candidate_sha256 =
digest(direction)`, `context_pages`, `signature: SignatureDraft | None`,
`reviews: SignatureReview[]`, `searches: TargetRetrieval[]`, `diagnostic`.

`TargetRetrieval` (`schemas.py:83`): `target_id`, `status`
`complete|partial|failed`, `retrieval` (the raw `RetrievalResult`), `ranked:
RankedMatch[]`, `paper_artifact_hashes`, `unavailable_papers`, `diagnostic`,
`novelty='not_assessed'`.

Persisted as `out/novelty_search.json` (or `novelty_search_recovery_N.json`
during recovery).

## What later stages rely on

- `comparison` groups `ranked` papers across targets into one shortlist per direction and audits each (target, paper) pair; `paper_artifact_hashes` must agree across targets.
- The signature's facets and `proposal_paths` define the "proposal requirements" the evidence audit checks against.
- A `failed` target (e.g. `dir-001-h02` in the October replay) leaves that hypothesis with no shortlist, so its coverage stays below threshold in `assessment`.

## Implementation vs design

§10 signature is implemented with a stricter provenance rule than designed
(every source-fact facet must quote a supplied page). §11 uses the same
retriever as stage 2 with LLM reranking; there is no separate embedding
index for novelty.
