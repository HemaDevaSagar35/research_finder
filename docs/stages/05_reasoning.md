# Stage 5 — `reasoning`

Design reference: architecture §5 Cross-Paper Reasoning.

## Aim

For each landscape item ("thread") decide what the papers collectively
establish: multi-paper findings, single-paper observations, and unresolved
tensions. Every accepted statement is grounded in code-selected evidence from
`paper.json` and verified against the original page text by an independent
review call. Everything that does not pass is kept as a typed diagnostic.

## Inputs

Entry point: `CrossPaperReasoner(store, chat, *, budgets, provider, draft_model, review_model, verify='pages', concurrency=4, index_dir=None, bundle_budget=None).run(land) -> CrossPaperReasoning`
— constructor `reasoning/cross_paper.py:370`, `run` at `:416`. Pipeline wiring
(`research/pipeline.py:160`): a dedicated `AsyncLLMClient(c.provider or
REASON_PROVIDER, concurrency=c.concurrency, max_retries=0)` whose
`chat_result` is injected; `budgets=c.reasoning`; `draft_model=c.model`;
`review_model=c.review_model`.

| Input | Contract |
|---|---|
| `land: Landscape` | Stage 4 output, validated by `validate_landscape` (`reasoning/schemas.py:165`). |
| `store: PaperStore` | Loads `paper.json` and page markdown for evidence and review. |
| `budgets: Budgets` (`cross_paper.py:70`) | `max_calls` (None), `max_threads` 100, `max_papers_per_thread` 12, `max_bundle_chars` (`REASON_MAX_BUNDLE_CHARS`, 2 000 000), `max_draft_input_tokens` / `max_draft_output_tokens` (`REASON_MAX_DRAFT_*`, 500 000), `max_review_pages` 16, `max_review_input_tokens` / `max_review_output_tokens` (`REASON_MAX_REVIEW_*`, 500 000), `repair_rounds` 1. |
| provider / models | `REASON_PROVIDER`, `REASON_MODEL`, `REASON_REVIEW_MODEL` as fallbacks. |

`PROMPT_VERSION = 'cross_paper_prompt_v2_attribution'` (`cross_paper.py:61`).

## Logic

1. `schedule_threads(land, budgets)` (`cross_paper.py:151`): one `Thread` per relationship, aggregated finding / limitation / assumption, under-explored regime, and contradiction, ids `t001, t002, ...`; papers per thread chosen by `_ordered` (supporting papers first, then same-group papers, capped). Threads past `max_threads` → `budget_skipped`. Contradiction threads whose side has <2 papers → `insufficient_for_adjudication`.
2. Per thread (`_process`, `cross_paper.py:445`), concurrently under a semaphore:
   1. Load papers via `store.get`; none loadable → `insufficient`.
   2. `candidates(paper, pid)` (`reasoning/evidence.py:236`) walks `PaperAnalysis` into `EvidenceCandidate`s with JSON-pointer `value_path`/`provenance_path`.
   3. `bundle(...)` (`evidence.py:403`) selects evidence under a per-paper character quota and labels items `t002-e001, ...`; omitted items are recorded explicitly (`OmittedItem`, reason `budget` / `input_tokens`).
   4. **Draft** (`_draft`, `:524`): `DRAFT_SYSTEM` (`:269`) + fenced JSON bundle → `ThreadReasoningDraft`; `repair_rounds` retries on structural/citation errors; input shrunk by dropping lowest-ranked items until under `max_draft_input_tokens`.
   5. `_resolve` (`:596`): map cited ids to `Evidence`; 1 paper → observation, ≥2 → finding; tensions need two sides with ≥2 distinct papers; bad citations → diagnostics.
   6. **Review** (`_review`, `:696`): rank pages by citation count, load page markdown (`page_id = f"{paper_id}#p{page}"`), cap by `max_review_pages` / tokens; `REVIEW_SYSTEM` (`:297`) → `SupportReviewResponse`; a response whose decision set does not match the candidates or cites an unknown page is invalid. Candidates whose required pages were not supplied → `insufficient_pages`.
   7. `_accept` (`:809`): ids `-cNN` → `-fNN` / `-oNN` / `-xNN`; `review_sources` are built from code-side page hashes, not model text; `agreement` denominator is the thread's scheduled papers; confidence low/medium/high by `_confidence`.
3. `_assemble` → `CrossPaperReasoning` with coverage counters, call snapshot and usage delta.

Status/diagnostic vocabulary: `DiagnosticReason` (`reasoning/schemas.py:478`)
— `budget_skipped`, `insufficient`, `insufficient_for_adjudication`, `failed`,
`invalid_citation`, `unresolved_location`, `unverifiable_location`,
`no_valid_evidence`, `insufficient_pages`, `rejected_on_review`,
`review_invalid`, `draft_invalid`, `verification_skipped`. A fatal provider
error fails the remaining threads.

## Outputs

`CrossPaperReasoning` — `reasoning/schemas.py:518` (`schema_version='cross_paper_v1'`).

| Field | Contract |
|---|---|
| `landscape_ref: {schema_version, sha256}` | Digest of the input landscape; checked by every later stage. |
| `run: RunInfo` | provider, draft/review models, prompt_version, budgets, verify_mode, token_counter, timestamps. |
| `findings: list[CrossPaperFinding]` | `finding_id`, `thread_id`, `refines` (landscape item ids), `finding_kind`, `statement`, `conditions`, `evidence: Evidence[]`, `agreement`, `confidence`, `verification='verified'`, `review_sources`, `review_notes`. |
| `observations: list[Observation]` | Same shape for single-paper statements (`observation_id`). |
| `tensions: list[Tension]` | `tension_id`, `sides`, `candidate_explanations`, `distinct_papers`. |
| `diagnostics: list[CandidateDiagnostic]` | Everything not accepted, with reason. |
| `coverage`, `usage` | Counters and token usage. |

`Evidence` (`schemas.py:385`): `evidence_id`, `paper_id`, `source_kind='paper_json'`,
`value_path`, `provenance_path`, `source_locations`, `source_value`,
`summary`, `stance`, `artifact_sha256`. `ReviewSource` (`:405`): `paper_id`,
`page`, `key_or_path`, `sha256`.

Persisted as `out/reasoning.json`.

## What later stages rely on

- Only items with `verification == 'verified'` and non-empty `review_sources` can be mined; `opportunities` namespaces them as `finding:t002-f01` and evidence as `observation:t002-o01/t002-e001`.
- `Evidence.artifact_sha256` and `ReviewSource.sha256` are re-checked on every reload; a mismatch is `stale_evidence`.
- `refines` ids must resolve to landscape `item_id`s.

## Implementation vs design

§5 is implemented as specified, with two additions not in the design: the
page-level support review as a mandatory second call, and explicit
`OmittedItem` accounting so that evidence not shown to the model is never
read as agreement.
