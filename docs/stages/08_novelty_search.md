# Stage 8 — `novelty_search`

Design reference: architecture §9 Novelty Search = Second RAG, §10 Novelty Signature, §11 Second RAG Retrieval. Implementation notes: `docs/novelty_search.md`.

## Aim

A direction was written from fifteen papers. The obvious risk is that paper
number sixteen, somewhere in the full index, already did it. This stage
begins the check. For the direction and for each hypothesis it first writes a
**signature** — a precise statement of what is being claimed, separated into
facts taken from the source papers and proposals that are the candidate's
own — and then uses that signature to search the *whole* index and shortlist
the prior papers most likely to overlap. It produces shortlists, not
verdicts: every output is stamped `novelty='not_assessed'`.

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

Prompt versions (`novelty/prompts.py`): `SIGNATURE_VERSION =
'novelty_signature_v4_direct_page_provenance'`, plus `REVIEW_VERSION`, `RANK_VERSION`.

## Logic

### Why a signature, and why it splits fact from proposal

Searching the index with the direction's title would find papers about the
same *topic*, which is not the question. The question is whether a prior
paper already *does what the candidate proposes*. To ask that precisely the
stage decomposes each target into facets — the problem, the intervention,
the expected effect, the decision signal, the mechanism, the regime, the
comparison — and labels every facet with its **basis**:

- `source_fact`: this comes from the source papers (and the facet must quote
  the page it comes from);
- `candidate_proposal`: this is the candidate's own idea (and the facet must
  point, by JSON pointer, at where in the proposal it is stated).

The split is what makes the later comparison meaningful. A prior paper that
matches only the `source_fact` facets is background the candidate already
cites; one that matches the `candidate_proposal` facets is a genuine overlap.
Each signature also carries the search queries that should find such papers.
(`Facet`, `TargetSignature`, `novelty/schemas.py:11-42`.)

### Step 1 — rebuild the context

The direction's source papers and context pages are reloaded and their
hashes checked against the direction; the payload is the proposal, the
opportunity, the paper artifacts and the page texts. (`_context`, `pipeline.py:211`.)

### Step 2 — write and review the signature

The model is asked for one signature per target (the direction and every
hypothesis), with the basis labelling above and up to `max_queries` queries
each. The code validates it: every target covered exactly once, pointers
resolve into the proposal, every `source_fact` quote is literally present on
a supplied page and the quoted pages cover every paper cited, queries unique.
Then an independent review call — with the review model — checks the
signature for correctness against the same pages: is a facet labelled
`source_fact` really stated by the source, or is it the candidate's inference
dressed as a fact? `pass` accepts; `revise` gives one revision and one more
review; a second non-pass or an `abstain` withholds the whole direction from
search. A direction with no accepted signature gets no shortlist at all — it
is better to say "not searched" than to search for the wrong thing.
(`_signature`, `pipeline.py:261`; `validate_signature`, `:113`; prompts
`SIGNATURE`, `REVIEW`.)

### Step 3 — search and rerank per target

For each target the signature's queries are run through the same retriever
as stage 2 but asking for many more papers (`candidate_k` = 100) — recall
matters more than precision here. Every returned paper's artifact is loaded;
papers whose `paper.json` is unavailable are listed as such and cannot be
ranked. The model then reads the signature and the loaded papers' cards and
returns the `rerank_k` (20) most likely to overlap, each with a sentence on
why. The code requires exactly that many unique, loadable ids — a rerank that
drops or invents a paper is invalid. The target's status is `complete` if
everything loaded, `partial` if some artifacts were unavailable, `failed` on
any exception. (`_search`, `pipeline.py:318`; prompt `RANK`; schema `RankingResponse`.)

### Call plumbing shared with later novelty stages

Every model call goes through `_call`: the request is a JSON body with a
`task` name; the input is checked against the token budget before sending; a
response the provider reports as *truncated* raises `TruncatedOutputError`
and is **not** resent unchanged (resending the same oversize input just
truncates again); unknown keys whose value is `null` are stripped and
recorded, because the provider's JSON-schema mode is advisory and models
occasionally emit optional keys as `null`. (`_call`, `pipeline.py:165`;
`drop_null_extras`, `:34`.)

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

- `comparison` merges the `ranked` lists of all targets of a direction into one shortlist and audits every (target, paper) pair; the signature's facets become the "proposal requirements" the evidence audit checks a paper against.
- A `failed` target has no shortlist, so in `assessment` its coverage is below threshold and the hypothesis cannot be approved (this is what happened to `dir-001-h02` in the October replay).

## Implementation vs design

§10 signature is implemented with a stricter provenance rule than designed
(every source-fact facet must quote a supplied page). §11 uses the same
retriever as stage 2 with model reranking; there is no separate embedding
index for novelty.
